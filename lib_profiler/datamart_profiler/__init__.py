import codecs
import hdbscan
import json
import logging
import math
import numpy
import os
import pandas
import pkg_resources
import prometheus_client
import random
import subprocess

from .identify_types import identify_types
from datamart_core.common import Type

logger = logging.getLogger(__name__)


MAX_SIZE = 50_000_000


BUCKETS = [0.5, 1.0, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0, 300.0, 600.0]

PROM_PROFILE = prometheus_client.Histogram('profile_seconds',
                                           "Profile time",
                                           buckets=BUCKETS)
PROM_TYPES = prometheus_client.Histogram('profile_types_seconds',
                                         "Profile types time",
                                         buckets=BUCKETS)
PROM_SPATIAL = prometheus_client.Histogram('profile_spatial_seconds',
                                           "Profile spatial coverage time",
                                           buckets=BUCKETS)


def mean_stddev(array):
    total = 0
    for elem in array:
        try:
            total += float(elem)
        except ValueError:
            pass
    mean = total / len(array)if len(array) > 0 else 0
    total = 0
    for elem in array:
        try:
            elem = float(elem) - mean
        except ValueError:
            continue
        total += elem * elem
    stddev = math.sqrt(total / len(array)) if len(array) > 0 else 0

    return mean, stddev


def get_numerical_ranges(values):
    """
    Retrieve the numeral ranges given the input (timestamp, integer, or float).
    """

    def get_ranges(values_):
        range_diffs = []
        for i in range(1, len(values_)):
            diff = values_[i][0] - values_[i - 1][1]
            diff != 0 and range_diffs.append(diff)

        avg_range_diff, std_dev_range_diff = mean_stddev(range_diffs)

        ranges = []
        current_min = values_[0][0]
        current_max = values_[0][1]

        for i in range(1, len(values_)):
            if (values_[i][0] - values_[i - 1][1]) > avg_range_diff + 2 * std_dev_range_diff:
                ranges.append([current_min, current_max])
                current_min = values_[i][0]
                current_max = values_[i][1]
                continue
            current_max = values_[i][1]
        ranges.append([current_min, current_max])

        return ranges

    if not values:
        return []

    values = [[v, v] for v in sorted(values)]
    # run it twice
    values = get_ranges(values)
    values = get_ranges(values)

    final_ranges = []
    for v in values:
        final_ranges.append({"range": {"gte": v[0], "lte": v[1]}})

    return final_ranges


def get_spatial_ranges(values):
    """
    Retrieve the spatial ranges (i.e. bounding boxes) given the input gps points.
    It uses HDBSCAN for finding finer spatial ranges.
    """

    def get_euclidean_distance(p1, p2):
        return numpy.linalg.norm(numpy.array(p1) - numpy.array(p2))

    def get_ranges(values_):
        range_diffs = []
        for j in range(1, len(values_)):
            diff = get_euclidean_distance(values_[j - 1][1], values_[j][1])
            diff != 0 and range_diffs.append(diff)

        avg_range_diff, std_dev_range_diff = mean_stddev(range_diffs)

        ranges = []
        current_bb = values_[0][0]
        current_point = values_[0][1]

        for j in range(1, len(values_)):
            dist = get_euclidean_distance(current_point, values_[j][1])
            if dist > avg_range_diff + 2 * std_dev_range_diff:
                ranges.append([current_bb, current_point])
                current_bb = values_[j][0]
                current_point = values_[j][1]
                continue
            current_bb = [[min(current_bb[0][0], values_[j][0][0][0]),  # min lat
                           max(current_bb[0][1], values_[j][0][0][1])],  # max lat
                          [min(current_bb[1][0], values_[j][0][1][0]),  # min lon
                           max(current_bb[1][1], values_[j][0][1][1])]]  # max lon
            current_point = [(current_bb[0][0] + current_bb[0][1])/2,
                             (current_bb[1][0] + current_bb[1][1])/2]
        ranges.append([current_bb, current_point])

        return ranges

    min_cluster_size = 10
    if len(values) <= min_cluster_size:
        min_cluster_size = 2

    clustering = hdbscan.HDBSCAN(min_cluster_size=min_cluster_size).fit(values)

    clusters = {}
    for i in range(len(values)):
        label = clustering.labels_[i]
        if label < 0:
            continue
        if label not in clusters:
            clusters[label] = [[float("inf"), -float("inf")], [float("inf"), -float("inf")]]
        clusters[label][0][0] = max(-90.0, min(clusters[label][0][0], values[i][0]))  # min lat
        clusters[label][0][1] = min(90.0, max(clusters[label][0][1], values[i][0]))  # max lat

        clusters[label][1][0] = max(-180.0, min(clusters[label][1][0], values[i][1]))  # min lon
        clusters[label][1][1] = min(180.0, max(clusters[label][1][1], values[i][1]))  # max lon

    # further clustering

    all_clusters = [v for v in clusters.values()
                    if (v[0][0] != v[0][1]) and (v[1][0] != v[1][1])]
    if not all_clusters:
        return None

    if len(all_clusters) == 1:
        cluster = all_clusters[0]
        return [{"range": {"type": "envelope",
                           "coordinates": [
                               [cluster[1][0], cluster[0][1]],
                               [cluster[1][1], cluster[0][0]]
                           ]}}]

    values = [(v, [(v[0][0] + v[0][1])/2, (v[1][0] + v[1][1])/2])
              for v in all_clusters]  # adding centroid

    # lat
    values = get_ranges(sorted(values, key=lambda v: (v[1][0], v[1][1])))
    # lon
    values = get_ranges(sorted(values, key=lambda v: (v[1][1], v[1][0])))

    final_ranges = []
    for v in values:
        final_ranges.append({"range": {"type": "envelope",
                                       "coordinates": [
                                           [v[0][1][0], v[0][0][1]],
                                           [v[0][1][1], v[0][0][0]]
                                       ]}})

    return final_ranges


def run_scdp(data):
    # Run SCDP
    logger.info("Running SCDP...")
    scdp = pkg_resources.resource_filename('datamart_profiler', 'scdp.jar')
    if isinstance(data, (str, bytes)):
        if os.path.isdir(data):
            data = os.path.join(data, 'main.csv')
        if not os.path.exists(data):
            raise ValueError("data file does not exist")
        proc = subprocess.Popen(['java', '-jar', scdp, data],
                                stdout=subprocess.PIPE,
                                stdin=subprocess.PIPE)
        stdout, _ = proc.communicate()
    else:
        proc = subprocess.Popen(['java', '-jar', scdp, '/dev/stdin'],
                                stdout=subprocess.PIPE,
                                stdin=subprocess.PIPE)
        data.to_csv(codecs.getwriter('utf-8')(proc.stdin))
        stdout, _ = proc.communicate()
    if proc.wait() != 0:
        logger.error("Error running SCDP: returned %d", proc.returncode)
        return {}
    else:
        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            logger.exception("Invalid output from SCDP")
            return {}


@PROM_PROFILE.time()
def process_dataset(
        data,
        dataset_id=None,
        metadata=None,
        lazo_client=None,
        search=False):
    """Compute all metafeatures from a dataset.

    :param data: path to dataset
    :param dataset_id: id of the dataset
    :param metadata: The metadata provided by the discovery plugin (might be
        very limited).
    :param lazo_client: client for the Lazo Index Server
    :param search: True if this method is being called during the search
        operation (and not for indexing).
    """
    if metadata is None:
        metadata = {}

    # FIXME: SCDP currently disabled
    # scdp_out = run_scdp(data)
    scdp_out = {}

    data_path = None
    if isinstance(data, (str, bytes)):
        if not os.path.exists(data):
            raise ValueError("data file does not exist")

        # saving path
        if isinstance(data, str):
            data_path = data

        # File size
        metadata['size'] = os.path.getsize(data)
        logger.info("File size: %r bytes", metadata['size'])

        # Sub-sample
        if metadata['size'] > MAX_SIZE:
            logger.info("Counting rows...")
            with open(data, 'rb') as fp:
                metadata['nb_rows'] = sum(1 for _ in fp)

            ratio = MAX_SIZE / metadata['size']
            logger.info("Loading dataframe, sample ratio=%r...", ratio)
            data = pandas.read_csv(
                data,
                dtype=str, na_filter=False,
                skiprows=lambda i: i != 0 and random.random() > ratio)
        else:
            logger.info("Loading dataframe...")
            data = pandas.read_csv(data,
                                   dtype=str, na_filter=False)

            metadata['nb_rows'] = data.shape[0]

        logger.info("Dataframe loaded, %d rows, %d columns",
                    data.shape[0], data.shape[1])
    else:
        if not isinstance(data, pandas.DataFrame):
            raise TypeError("data should be a filename or a pandas.DataFrame")
        metadata['nb_rows'] = len(data)

    # Get column dictionary
    columns = metadata.setdefault('columns', [])
    # Fix size if wrong
    if len(columns) != len(data.columns):
        logger.info("Setting column names from header")
        columns[:] = [{} for _ in range(len(data.columns))]
    else:
        logger.info("Keeping columns from discoverer")

    # Set column names
    for column_meta, name in zip(columns, data.columns):
        column_meta['name'] = name

    # Copy info from SCDP
    for column_meta, name in zip(columns, data.columns):
        column_meta.update(scdp_out.get(name, {}))

    # Lat / Lon
    column_lat = []
    column_lon = []

    # Textual columns
    column_textual = []

    # Identify types
    logger.info("Identifying types...")
    with PROM_TYPES.time():
        for i, column_meta in enumerate(columns):
            array = data.iloc[:, i]
            structural_type, semantic_types_dict = \
                identify_types(array, column_meta['name'])
            # Set structural type
            column_meta['structural_type'] = structural_type
            # Add semantic types to the ones already present
            sem_types = column_meta.setdefault('semantic_types', [])
            for sem_type in semantic_types_dict:
                if sem_type not in sem_types:
                    sem_types.append(sem_type)

            if structural_type in (Type.INTEGER, Type.FLOAT):
                column_meta['mean'], column_meta['stddev'] = mean_stddev(array)

                # Get numerical ranges
                # logger.warning(" Column Name: " + column_meta['name'])
                numerical_values = []
                for e in array:
                    try:
                        numerical_values.append(float(e))
                    except ValueError:
                        numerical_values.append(None)

                # Get lat/lon columns
                if Type.LATITUDE in semantic_types_dict:
                    column_lat.append(
                        (column_meta['name'], numerical_values)
                    )
                elif Type.LONGITUDE in semantic_types_dict:
                    column_lon.append(
                        (column_meta['name'], numerical_values)
                    )
                else:
                    column_meta['coverage'] = get_numerical_ranges(
                        [x for x in numerical_values if x is not None]
                    )

            if Type.DATE_TIME in semantic_types_dict:
                timestamps = numpy.empty(
                    len(semantic_types_dict[Type.DATE_TIME]),
                    dtype='float32',
                )
                timestamps_for_range = []
                for j, dt in enumerate(
                        semantic_types_dict[Type.DATE_TIME]):
                    timestamps[j] = dt.timestamp()
                    timestamps_for_range.append(
                        dt.replace(minute=0, second=0).timestamp()
                    )
                column_meta['mean'], column_meta['stddev'] = \
                    mean_stddev(timestamps)

                # Get temporal ranges
                column_meta['coverage'] = \
                    get_numerical_ranges(timestamps_for_range)

            if structural_type == Type.TEXT and \
                    Type.DATE_TIME not in semantic_types_dict:
                column_textual.append(column_meta['name'])

    # Textual columns
    if lazo_client and column_textual:
        # Indexing with lazo
        if not search:
            logger.info("Indexing textual data with Lazo...")
            try:
                if data_path:
                    # if we have the path, send the path
                    lazo_client.index_data_path(
                        data_path,
                        dataset_id,
                        column_textual
                    )
                else:
                    # if path is not available, send the data instead
                    for column_name in column_textual:
                        lazo_client.index_data(
                            data[column_name].values.tolist(),
                            dataset_id,
                            column_name
                        )
            except Exception as e:
                logger.warning('Error indexing textual attributes from %s', dataset_id)
                logger.warning(str(e))
        # Generating Lazo sketches for the search
        else:
            logger.info("Generating Lazo sketches...")
            try:
                if data_path:
                    # if we have the path, send the path
                    lazo_sketches = lazo_client.get_lazo_sketch_from_data_path(
                        data_path,
                        "",
                        column_textual
                    )
                else:
                    # if path is not available, send the data instead
                    lazo_sketches = []
                    for column_name in column_textual:
                        lazo_sketches.append(
                            lazo_client.get_lazo_sketch_from_data(
                                data[column_name].values.tolist(),
                                "",
                                column_name
                            )
                        )
                ## saving sketches into metadata
                metadata_lazo = []
                for i in range(len(column_textual)):
                    n_permutations, hash_values, cardinality =\
                        lazo_sketches[i]
                    metadata_lazo.append(dict(
                        name=column_textual[i],
                        n_permutations=n_permutations,
                        hash_values=list(hash_values),
                        cardinality=cardinality
                    ))
                metadata['lazo'] = metadata_lazo
            except Exception as e:
                logger.warning('Error getting Lazo sketches textual attributes from %s', dataset_id)
                logger.warning(str(e))

    # Lat / Lon
    logger.info("Computing spatial coverage...")
    with PROM_SPATIAL.time():
        spatial_coverage = []
        i_lat = i_lon = 0
        while i_lat < len(column_lat) and i_lon < len(column_lon):
            name_lat = column_lat[i_lat][0]
            name_lon = column_lon[i_lon][0]

            values_lat = column_lat[i_lat][1]
            values_lon = column_lon[i_lon][1]
            values = []
            for i in range(len(values_lat)):
                if values_lat[i] is not None and values_lon[i] is not None:
                    values.append((values_lat[i], values_lon[i]))

            if len(values) > 1:
                spatial_ranges = get_spatial_ranges(list(set(values)))
                if spatial_ranges:
                    spatial_coverage.append({"lat": name_lat,
                                             "lon": name_lon,
                                             "ranges": spatial_ranges})

            i_lat += 1
            i_lon += 1

    if spatial_coverage:
        metadata['spatial_coverage'] = spatial_coverage

    # Return it -- it will be inserted into Elasticsearch, and published to the
    # feed and the waiting on-demand searches
    return metadata
