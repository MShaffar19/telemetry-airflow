"""
This trains an ensemble model for TAAR
based on a set of constituent recommenders (collaborative, locale and
similarity).

We take firefox client_info data from the clients_daily table and
and obtain the most recent data.

For each client with N addons, We mask the most recently installed
addon to use as the best suggestion.  Using the N-1 addon list - we
generate recommendations for each of the 3 base models outputting
GUID and weight for each recommendation.

We compute CLLR values substituting in 0 in the edge case where CLLR
computes a NaN value for the recommendation set from each recommender.

We then compute a Vector with (has_match, weight=1.0,
features=[cllr_1, cllr_2, cllr_3]) and then train a LogisticRegression
model to compute coefficients for each of the recommenders.
"""

import click
import boto3
import json
import numpy as np
import os
import sys
import tempfile
import contextlib
import shutil

from datetime import date, timedelta
from importlib import reload
from pyspark.ml.classification import LogisticRegression
from pyspark.ml.linalg import Vectors
from pyspark.sql import Row
from pyspark.sql import SparkSession
from pyspark.sql.functions import udf, size, rand
from pyspark.sql.types import ArrayType
from pyspark.sql.types import StringType
from pyspark import SparkConf
from taar.context import default_context


# Define the set of feature names to be used in the donor computations.
CATEGORICAL_FEATURES = ["geo_city", "locale", "os"]
CONTINUOUS_FEATURES = [
    "subsession_length",
    "bookmark_count",
    "tab_open_count",
    "total_uri",
    "unique_tlds",
]


def get_df(spark, date_from, sample_rate):
    gs_url = "gs://moz-fx-data-derived-datasets-parquet/clients_daily/v6"
    parquetFile = spark.read.parquet(gs_url)
    # Use the parquet files to create a temporary view and then used in SQL statements.
    parquetFile.createOrReplaceTempView("clients_daily")
    df = (
        spark.sql("SELECT * FROM clients_daily")
        .where("active_addons IS NOT null")
        .where("size(active_addons) > 2")
        .where("size(active_addons) < 100")
        .where("channel = 'release'")
        .where("app_name = 'Firefox'")
        .where("submission_date_s3 >= {}".format(date_from))
        .selectExpr(
            "client_id as client_id",
            "active_addons as active_addons",
            "city as geo_city",
            "subsession_hours_sum as subsession_length",
            "locale as locale",
            "os as os",
            "row_number() OVER (PARTITION BY client_id ORDER BY submission_date_s3 desc) as rn",
            "places_bookmarks_count_mean AS bookmark_count",
            "scalar_parent_browser_engagement_tab_open_event_count_sum AS tab_open_count",
            "scalar_parent_browser_engagement_total_uri_count_sum AS total_uri",
            "scalar_parent_browser_engagement_unique_domains_count_max AS unique_tlds",
        )
        .where("rn = 1")
        .drop("rn")
    ).sample(False, sample_rate)
    return df


def get_addons_per_client(users_df, minimum_addons_count):
    """ Extracts a DataFrame that contains one row
    for each client along with the list of active add-on GUIDs.
    """

    def is_valid_addon(addon):
        return not (
            addon.is_system
            or addon.app_disabled
            or addon.type != "extension"
            or addon.user_disabled
            or addon.foreign_install
            or addon.install_day is None
        )

    # may need additional whitelisting to remove shield addons

    def get_valid_addon_ids(addons):
        sorted_addons = sorted(
            [(a.addon_id, a.install_day) for a in addons if is_valid_addon(a)],
            key=lambda addon_tuple: addon_tuple[1],
        )
        return [addon_id for (addon_id, install_day) in sorted_addons]

    get_valid_addon_ids_udf = udf(get_valid_addon_ids, ArrayType(StringType()))

    # Create an add-ons dataset un-nesting the add-on map from each
    # user to a list of add-on GUIDs. Also filter undesired add-ons.
    return users_df.select(
        "client_id", get_valid_addon_ids_udf("active_addons").alias("addon_ids")
    ).filter(size("addon_ids") > minimum_addons_count)


def safe_get_int(row, fieldname, default, factor=None):
    tmp = getattr(row, fieldname, default)
    if tmp is None:
        return 0
    try:
        if factor is not None:
            tmp *= factor
        tmp = int(tmp)
    except Exception:
        return 0
    return tmp


def safe_get_str(row, fieldname):
    tmp = getattr(row, fieldname, "")
    if tmp is None:
        return ""
    return str(tmp)


def row_to_json(row):
    jdata = {}

    # This is not entirely obvious.  All of our row data from raw telemetry uses *real*
    # client_ids.   The production TAAR system only uses hashed telemetry client IDs.
    # That said - we don't need to hash because we are only concerned
    # with GUID recommendations here for the purposes of training
    jdata["client_id"] = row.client_id

    # Note the inconsistent naming of the addon ID field
    jdata["installed_addons"] = row.addon_ids
    jdata["bookmark_count"] = safe_get_int(row, "bookmark_count", 0)
    jdata["tab_open_count"] = safe_get_int(row, "tab_open_count", 0)
    jdata["total_uri"] = safe_get_int(row, "total_uri", 0)
    jdata["subsession_length"] = safe_get_int(row, "subsession_length", 0, 3600)
    jdata["unique_tlds"] = safe_get_int(row, "unique_tlds", 0)
    jdata["geo_city"] = safe_get_str(row, "geo_city")
    jdata["locale"] = safe_get_str(row, "locale")
    jdata["os"] = safe_get_str(row, "os")

    return jdata


def reload_configuration():
    """
    Configuration needs to be reloaded on a per worker node basis.
    This is an unfortunate a side effect of re-using the TAAR library which
    expects to be using python-decouple to load the configuration
    from enviroment variables.
    """
    from taar.recommenders import s3config

    # Locale Recommender Overrides
    # This *Must* be called just prior to instantiating the individual recommenders in the
    # ETL enviroment.
    s3config.TAAR_LOCALE_BUCKET = os.environ[
        "TAAR_LOCALE_BUCKET"
    ] = "telemetry-parquet"
    s3config.TAAR_LOCALE_KEY = os.environ[
        "TAAR_LOCALE_KEY"
    ] = "taar/locale/top10_dict.json"

    # Similarity Recommender configuration overrides
    s3config.TAAR_SIMILARITY_BUCKET = os.environ[
        "TAAR_SIMILARITY_BUCKET"
    ] = "telemetry-parquet"
    s3config.TAAR_SIMILARITY_DONOR_KEY = os.environ[
        "TAAR_SIMILARITY_DONOR_KEY"
    ] = "taar/similarity/donors.json"
    s3config.TAAR_SIMILARITY_LRCURVES_KEY = os.environ[
        "TAAR_SIMILARITY_LRCURVES_KEY"
    ] = "taar/similarity/lr_curves.json"

    # Collaborative Recommender Overrides
    s3config.TAAR_ITEM_MATRIX_BUCKET = os.environ[
        "TAAR_ITEM_MATRIX_BUCKET"
    ] = "telemetry-public-analysis-2"
    s3config.TAAR_ITEM_MATRIX_KEY = os.environ[
        "TAAR_ITEM_MATRIX_KEY"
    ] = "telemetry-ml/addon_recommender/item_matrix.json"
    s3config.TAAR_ADDON_MAPPING_BUCKET = os.environ[
        "TAAR_ADDON_MAPPING_BUCKET"
    ] = "telemetry-public-analysis-2"
    s3config.TAAR_ADDON_MAPPING_KEY = os.environ[
        "TAAR_ADDON_MAPPING_KEY"
    ] = "telemetry-ml/addon_recommender/addon_mapping.json"

    from taar.recommenders import LocaleRecommender
    from taar.recommenders import SimilarityRecommender
    from taar.recommenders import CollaborativeRecommender

    reload(sys.modules["taar.recommenders"])

    # Force reload of recommender modules
    [
        reload(sys.modules[rec_cls.__module__])
        for rec_cls in [
            LocaleRecommender,
            SimilarityRecommender,
            CollaborativeRecommender,
        ]
    ]


COLLABORATIVE, SIMILARITY, LOCALE = "collaborative", "similarity", "locale"
PREDICTOR_ORDER = [COLLABORATIVE, SIMILARITY, LOCALE]


def load_recommenders(ctx):
    from taar.recommenders import LocaleRecommender
    from taar.recommenders import SimilarityRecommender
    from taar.recommenders import CollaborativeRecommender

    ctx = default_context()

    reload_configuration()

    lr = LocaleRecommender(ctx)
    sr = SimilarityRecommender(ctx)
    cr = CollaborativeRecommender(ctx)
    return {LOCALE: lr, COLLABORATIVE: cr, SIMILARITY: sr}


# Make predictions with sub-models and construct a new stacked row
def to_stacked_row(recommender_list, client_row):
    # Build a Row object with a label indicating
    # 1 or 0 for a match within at least one recommender.
    # Weight is set to 1.0 as the features will use a cllr result
    # indicating 'matchiness' with the known truth.
    try:
        training_client_info = row_to_json(client_row)

        # Pop off a single addon as the expected set.
        # I've tried a couple variations on this (pop 1 item, pop 2 items)
        # but there isn't much effect.

        expected = [training_client_info["installed_addons"].pop()]

        stacked_row = []

        cLLR = CostLLR()

        for recommend in recommender_list:
            guid_weight_list = recommend(training_client_info, limit=4)
            cllr_val = cLLR.evalcllr(guid_weight_list, expected)
            stacked_row.append(cllr_val)

        return Row(
            label=int(cLLR.total > 0.0),
            weight=1.0,
            features=Vectors.dense(*stacked_row),
        )
    except Exception:
        # This shouldn't happen. Log relevant data so that we can
        # Patch this up on the next run
        return None


# Stack the prediction results for each recommender into a stacked_row for each
# client_info blob in the training set.


def build_stacked_datasets(ctx, dataset, folds):
    # For each of k_folds, we apply the stacking
    # function to the training fold.
    # Where k_folds = 3, this will yield a list consisting
    # of 3 RDDs.   Each RDD is defined by the output of the
    # `stacking` function.

    def stacked_row_closure():
        rec_map = load_recommenders(ctx)

        recommender_list = [
            rec_map[COLLABORATIVE].recommend,  # Collaborative
            rec_map[SIMILARITY].recommend,  # Similarity
            rec_map[LOCALE].recommend,  # Locale
        ]

        def inner(client_row):
            return to_stacked_row(recommender_list, client_row)

        return inner

    wrapped_to_stacked_row = stacked_row_closure()

    print("Number of folds: {}".format(len(folds)))

    stacked_datasets = []
    for fold in folds:
        train_set = [f for f in folds if f != fold]
        stacking_result = [
            df.rdd.map(wrapped_to_stacked_row).filter(lambda x: x is not None)
            for df in train_set
        ]
        stacked_datasets.append(stacking_result)
    return stacked_datasets


def dump_training_info(blorModel):
    """
    This function is useful for debugging when we do not converge to a
    solution during LogisticRegression.
    """
    trainingSummary = blorModel.summary

    print("Total iterations: %d" % trainingSummary.totalIterations)
    print("Intercepts: " + str(blorModel.intercept))
    print("Coefficients: " + str(blorModel.coefficients))
    # Obtain the objective per iteration
    objectiveHistory = trainingSummary.objectiveHistory
    print("objectiveHistory:")
    for objective in objectiveHistory:
        print(objective)


def today_minus_7_days():
    return (date.today() + timedelta(days=-7)).strftime("%Y%m%d")


def verify_valid_coefs(coefs):
    """ verify that the model has proper floating point values (> 0)
    """

    assert "ensemble_weights" in coefs
    weights = coefs["ensemble_weights"]

    assert len(weights) == 3

    for key in weights.keys():
        assert key in coefs["ensemble_weights"]
        assert not np.isnan(coefs["ensemble_weights"][key])
        assert coefs["ensemble_weights"][key] > 0.0

    # This ordering must be strict
    msg = """
    FINAL WEIGHTS
    =============
    Collab     : {:0.8f}
    Locale     : {:0.8f}
    Similarity : {:0.8f}
    """.format(
        weights["collaborative"], weights["locale"], weights["similarity"]
    )

    print("Weight output")
    print("================================")
    print(msg)
    print("================================")
    assert weights["collaborative"] > 0.0
    assert weights["locale"] > 0.0
    assert weights["similarity"] > 0.0


class CostLLR:
    """ based on Niko Brummer's original implementation:
        Niko Brummer and Johan du Preez, Application-Independent Evaluation of Speaker Detection"
        Computer Speech and Language, 2005
    """

    def __init__(self):
        self._total = 0

    # evalcllr expects two lists
    # recommendations_list should be a list of (guid, weight) 2-tuples
    # unmasked_addons should be a list of guid strings
    def evalcllr(self, recommendations_list, unmasked_addons):
        # Organizer function to extract weights from recommendation list for passing to cllr.
        lrs_on_target_helper = np.array(
            [
                item[1]
                for item in recommendations_list
                if item[0] in unmasked_addons
            ]
        )
        lrs_off_target_helper = np.array(
            [
                item[1]
                for item in recommendations_list
                if item[0] not in unmasked_addons
            ]
        )
        try:
            tmp = self._cllr(lrs_on_target_helper, lrs_off_target_helper)
        except Exception:
            tmp = np.NaN

        if np.isnan(tmp):
            # This may happen if recommendations come back with a
            # weight of 0
            tmp = 0
        self._total += tmp
        return tmp

    @property
    def total(self):
        return self._total

    # Private methods below

    # Helper function to do some math for cllr.
    def _neg_log_sig(self, log_odds):
        neg_log_odds = [-1.0 * x for x in log_odds]
        e = np.exp(neg_log_odds)
        return [np.log(1 + f) for f in e if f < (f + 1)]

    # Compute the log likelihood ratio cost which should be minimized.
    def _cllr(self, lrs_on_target, lrs_off_target):
        lrs_on_target = np.log(lrs_on_target[~np.isnan(lrs_on_target)])
        lrs_off_target = np.log(lrs_off_target[~np.isnan(lrs_off_target)])

        c1 = np.mean(self._neg_log_sig(lrs_on_target)) / np.log(2)
        c2 = np.mean(self._neg_log_sig(-1.0 * lrs_off_target)) / np.log(2)
        return (c1 + c2) / 2


def cross_validation_split(dataset, k_folds):
    """
  Splits dataframe into k_folds, returning array of dataframes
  """
    dataset_split = []
    h = 1.0 / k_folds
    df = dataset.select("*", rand().alias("rand"))

    for i in range(k_folds):
        validateLB = i * h
        validateUB = (i + 1) * h
        condition = (df["rand"] >= validateLB) & (df["rand"] < validateUB)
        fold = df.filter(condition).cache()
        dataset_split.append(fold)

    return dataset_split


def verify_counts(taar_training, addons_info_df, client_samples_df):
    # This verification is only run to debug the job
    taar_training_count = taar_training.count()
    addons_info_count = addons_info_df.count()
    client_samples_count = client_samples_df.count()

    assert taar_training_count != client_samples_count
    assert taar_training_count == addons_info_count
    assert taar_training_count != client_samples_count
    # taar training should contain exactly the same number of elements
    # in addons_info_frame it should have filtered out clients that
    # started in client_features_frame

    print(
        "All counts verified.  taar_training_count == %d" % taar_training_count
    )


def extract(spark, date_from, minInstalledAddons, sample_rate):
    client_samples_df = get_df(spark, date_from, sample_rate)
    addons_info_df = get_addons_per_client(
        client_samples_df, minInstalledAddons
    )
    taar_training = addons_info_df.join(client_samples_df, "client_id", "inner")
    # verify_counts(taar_training, addons_info_df, client_samples_df)
    return taar_training


def compute_regression(spark, rdd_list, regParam, elasticNetParam):
    df0 = spark.sparkContext.union(rdd_list).toDF()
    blor = LogisticRegression(
        maxIter=50,
        regParam=regParam,
        weightCol="weight",
        elasticNetParam=elasticNetParam,
    )

    blorModel = blor.fit(df0)
    return blorModel


def transform(ctx, spark, taar_training, regParam, elasticNetParam):
    k_folds = 4
    df_folds = cross_validation_split(taar_training, k_folds)

    stacked_datasets_rdd_list = build_stacked_datasets(
        ctx, taar_training, df_folds
    )

    # Merge a list of RDD lists into a single RDD and then cast it into a DataFrame
    rdd_list = [
        spark.sparkContext.union(rdd_list)
        for rdd_list in stacked_datasets_rdd_list
    ]

    blorModel = compute_regression(spark, rdd_list, regParam, elasticNetParam)

    coefs = {
        "ensemble_weights": dict(
            [(k, v) for k, v in zip(PREDICTOR_ORDER, blorModel.coefficients)]
        )
    }

    verify_valid_coefs(coefs)

    return coefs


@contextlib.contextmanager
def selfdestructing_path(dirname):
    yield dirname
    shutil.rmtree(dirname)


def store_json_to_s3(json_data, base_filename, date, prefix, bucket):
    """Saves the JSON data to a local file and then uploads it to S3.

    Two copies of the file will get uploaded: one with as "<base_filename>.json"
    and the other as "<base_filename><YYYYMMDD>.json" for backup purposes.

    :param json_data: A string with the JSON content to write.
    :param base_filename: A string with the base name of the file to use for saving
        locally and uploading to S3.
    :param date: A date string in the "YYYYMMDD" format.
    :param prefix: The S3 prefix.
    :param bucket: The S3 bucket name.
    """

    tempdir = tempfile.mkdtemp()

    with selfdestructing_path(tempdir):
        JSON_FILENAME = "{}.json".format(base_filename)
        FULL_FILENAME = os.path.join(tempdir, JSON_FILENAME)
        with open(FULL_FILENAME, "w+") as json_file:
            json_file.write(json_data)

        archived_file_copy = "{}{}.json".format(base_filename, date)

        # Store a copy of the current JSON with datestamp.
        write_to_s3(FULL_FILENAME, archived_file_copy, prefix, bucket)
        write_to_s3(FULL_FILENAME, JSON_FILENAME, prefix, bucket)


def write_to_s3(source_file_name, s3_dest_file_name, s3_prefix, bucket):
    """Store the new json file containing current top addons per locale to S3.

    :param source_file_name: The name of the local source file.
    :param s3_dest_file_name: The name of the destination file on S3.
    :param s3_prefix: The S3 prefix in the bucket.
    :param bucket: The S3 bucket.
    """
    client = boto3.client("s3", "us-west-2")
    transfer = boto3.s3.transfer.S3Transfer(client)

    # Update the state in the analysis bucket.
    key_path = s3_prefix + s3_dest_file_name
    transfer.upload_file(source_file_name, bucket, key_path)


def load(coefs, date, prefix, bucket):
    store_json_to_s3(
        json.dumps(coefs, indent=2),
        "ensemble_weight",
        date,
        prefix,
        bucket,
    )


@click.command()
@click.option("--date", required=True)
@click.option("--aws_access_key_id", required=True)
@click.option("--aws_secret_access_key", required=True)
@click.option("--bucket", default="telemetry-parquet")
@click.option("--prefix", default="taar/ensemble/")
@click.option("--elastic_net_param", default=0.01)
@click.option("--reg_param", default=0.1)
@click.option("--min_installed_addons", default=4)
@click.option("--client_sample_date_from", default=today_minus_7_days())
@click.option("--sample_rate", default=0.005)
def main(
    date,
    aws_access_key_id,
    aws_secret_access_key,
    bucket,
    prefix,
    elastic_net_param,
    reg_param,
    min_installed_addons,
    client_sample_date_from,
    sample_rate,
):
    print("Sampling clients since {}".format(client_sample_date_from))

    # Clobber the AWS access credentials
    os.environ["AWS_ACCESS_KEY_ID"] = aws_access_key_id
    os.environ["AWS_SECRET_ACCESS_KEY"] = aws_secret_access_key

    ctx = default_context()

    APP_NAME = "TaarEnsemble"
    conf = SparkConf().setAppName(APP_NAME)
    spark = SparkSession.builder.config(conf=conf).getOrCreate()

    taar_training = extract(
        spark, client_sample_date_from, min_installed_addons, sample_rate
    )
    coefs = transform(ctx, spark, taar_training, reg_param, elastic_net_param)
    load(coefs, date, prefix, bucket)


if __name__ == "__main__":
    main()
