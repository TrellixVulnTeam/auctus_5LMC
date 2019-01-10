import contextlib
import datamart_materialize
import logging
import os
import shutil
import tempfile

from .discovery import encode_dataset_id


logger = logging.getLogger(__name__)


@contextlib.contextmanager
def get_dataset(metadata, dataset_id, format='csv'):
    shared = os.path.join('/datasets', encode_dataset_id(dataset_id))
    if os.path.exists(shared):
        yield shared
    else:
        temp_dir = tempfile.mkdtemp()
        try:
            temp_file = os.path.join(temp_dir, 'data')
            datamart_materialize.download(metadata, temp_file, None,
                                          format=format)
            yield temp_file
        finally:
            shutil.rmtree(temp_dir)
