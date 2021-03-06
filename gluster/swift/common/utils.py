# Copyright (c) 2012-2013 Red Hat, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import stat
import errno
import xattr
import random
import logging
from hashlib import md5
from eventlet import sleep
import cPickle as pickle
from swift.common.utils import normalize_timestamp
from gluster.swift.common.fs_utils import do_rename, do_fsync, os_path, \
    do_stat, do_listdir, do_walk, do_rmdir
from gluster.swift.common import Glusterfs

X_CONTENT_TYPE = 'Content-Type'
X_CONTENT_LENGTH = 'Content-Length'
X_TIMESTAMP = 'X-Timestamp'
X_PUT_TIMESTAMP = 'X-PUT-Timestamp'
X_TYPE = 'X-Type'
X_ETAG = 'ETag'
X_OBJECTS_COUNT = 'X-Object-Count'
X_BYTES_USED = 'X-Bytes-Used'
X_CONTAINER_COUNT = 'X-Container-Count'
X_OBJECT_TYPE = 'X-Object-Type'
DIR_TYPE = 'application/directory'
ACCOUNT = 'Account'
METADATA_KEY = 'user.swift.metadata'
MAX_XATTR_SIZE = 65536
CONTAINER = 'container'
DIR_NON_OBJECT = 'dir'
DIR_OBJECT = 'marker_dir'
TEMP_DIR = 'tmp'
ASYNCDIR = 'async_pending'  # Keep in sync with swift.obj.server.ASYNCDIR
FILE = 'file'
FILE_TYPE = 'application/octet-stream'
OBJECT = 'Object'
DEFAULT_UID = -1
DEFAULT_GID = -1
PICKLE_PROTOCOL = 2
CHUNK_SIZE = 65536


class GlusterFileSystemOSError(OSError):
    # Having our own class means the name will show up in the stack traces
    # recorded in the log files.
    pass


class GlusterFileSystemIOError(IOError):
    # Having our own class means the name will show up in the stack traces
    # recorded in the log files.
    pass


def read_metadata(path_or_fd):
    """
    Helper function to read the pickled metadata from a File/Directory.

    :param path_or_fd: File/Directory path or fd from which to read metadata.

    :returns: dictionary of metadata
    """
    metadata = None
    metadata_s = ''
    key = 0
    while metadata is None:
        try:
            metadata_s += xattr.getxattr(path_or_fd,
                                         '%s%s' % (METADATA_KEY, (key or '')))
        except IOError as err:
            if err.errno == errno.ENODATA:
                if key > 0:
                    # No errors reading the xattr keys, but since we have not
                    # been able to find enough chunks to get a successful
                    # unpickle operation, we consider the metadata lost, and
                    # drop the existing data so that the internal state can be
                    # recreated.
                    clean_metadata(path_or_fd)
                # We either could not find any metadata key, or we could find
                # some keys, but were not successful in performing the
                # unpickling (missing keys perhaps)? Either way, just report
                # to the caller we have no metadata.
                metadata = {}
            else:
                # Note that we don't touch the keys on errors fetching the
                # data since it could be a transient state.
                raise GlusterFileSystemIOError(
                    err.errno, 'xattr.getxattr("%s", %s)' % (path_or_fd, key))
        else:
            try:
                # If this key provides all or the remaining part of the pickle
                # data, we don't need to keep searching for more keys. This
                # means if we only need to store data in N xattr key/value
                # pair, we only need to invoke xattr get N times. With large
                # keys sizes we are shooting for N = 1.
                metadata = pickle.loads(metadata_s)
                assert isinstance(metadata, dict)
            except EOFError, pickle.UnpicklingError:
                # We still are not able recognize this existing data collected
                # as a pickled object. Make sure we loop around to try to get
                # more from another xattr key.
                metadata = None
                key += 1
    return metadata


def write_metadata(path_or_fd, metadata):
    """
    Helper function to write pickled metadata for a File/Directory.

    :param path_or_fd: File/Directory path or fd to write the metadata
    :param metadata: dictionary of metadata write
    """
    assert isinstance(metadata, dict)
    metastr = pickle.dumps(metadata, PICKLE_PROTOCOL)
    key = 0
    while metastr:
        try:
            xattr.setxattr(path_or_fd,
                           '%s%s' % (METADATA_KEY, key or ''),
                           metastr[:MAX_XATTR_SIZE])
        except IOError as err:
            raise GlusterFileSystemIOError(
                err.errno,
                'xattr.setxattr("%s", %s, metastr)' % (path_or_fd, key))
        metastr = metastr[MAX_XATTR_SIZE:]
        key += 1


def clean_metadata(path_or_fd):
    key = 0
    while True:
        try:
            xattr.removexattr(path_or_fd, '%s%s' % (METADATA_KEY, (key or '')))
        except IOError as err:
            if err.errno == errno.ENODATA:
                break
            raise GlusterFileSystemIOError(
                err.errno, 'xattr.removexattr("%s", %s)' % (path_or_fd, key))
        key += 1


def check_user_xattr(path):
    if not os_path.exists(path):
        return False
    try:
        xattr.setxattr(path, 'user.test.key1', 'value1')
    except IOError as err:
        raise GlusterFileSystemIOError(
            err.errno,
            'xattr.setxattr("%s", "user.test.key1", "value1")' % (path,))
    try:
        xattr.removexattr(path, 'user.test.key1')
    except IOError as err:
        logging.exception("check_user_xattr: remove failed on %s err: %s",
                          path, str(err))
        #Remove xattr may fail in case of concurrent remove.
    return True


def validate_container(metadata):
    if not metadata:
        logging.warn('validate_container: No metadata')
        return False

    if X_TYPE not in metadata.keys() or \
       X_TIMESTAMP not in metadata.keys() or \
       X_PUT_TIMESTAMP not in metadata.keys() or \
       X_OBJECTS_COUNT not in metadata.keys() or \
       X_BYTES_USED not in metadata.keys():
        return False

    (value, timestamp) = metadata[X_TYPE]
    if value == CONTAINER:
        return True

    logging.warn('validate_container: metadata type is not CONTAINER (%r)',
                 value)
    return False


def validate_account(metadata):
    if not metadata:
        logging.warn('validate_account: No metadata')
        return False

    if X_TYPE not in metadata.keys() or \
       X_TIMESTAMP not in metadata.keys() or \
       X_PUT_TIMESTAMP not in metadata.keys() or \
       X_OBJECTS_COUNT not in metadata.keys() or \
       X_BYTES_USED not in metadata.keys() or \
       X_CONTAINER_COUNT not in metadata.keys():
        return False

    (value, timestamp) = metadata[X_TYPE]
    if value == ACCOUNT:
        return True

    logging.warn('validate_account: metadata type is not ACCOUNT (%r)',
                 value)
    return False


def validate_object(metadata):
    if not metadata:
        logging.warn('validate_object: No metadata')
        return False

    if X_TIMESTAMP not in metadata.keys() or \
       X_CONTENT_TYPE not in metadata.keys() or \
       X_ETAG not in metadata.keys() or \
       X_CONTENT_LENGTH not in metadata.keys() or \
       X_TYPE not in metadata.keys() or \
       X_OBJECT_TYPE not in metadata.keys():
        return False

    if metadata[X_TYPE] == OBJECT:
        return True

    logging.warn('validate_object: metadata type is not OBJECT (%r)',
                 metadata[X_TYPE])
    return False


def _update_list(path, cont_path, src_list, reg_file=True, object_count=0,
                 bytes_used=0, obj_list=[]):
    # strip the prefix off, also stripping the leading and trailing slashes
    obj_path = path.replace(cont_path, '').strip(os.path.sep)

    for obj_name in src_list:
        # If it is not a reg_file then it is a directory.
        if not reg_file and not Glusterfs._implicit_dir_objects:
            # Now check if this is a dir object or a gratuiously crated
            # directory
            metadata = \
                read_metadata(os.path.join(cont_path, obj_path, obj_name))
            if not dir_is_object(metadata):
                continue

        if obj_path:
            obj_list.append(os.path.join(obj_path, obj_name))
        else:
            obj_list.append(obj_name)

        object_count += 1

        if reg_file and Glusterfs._do_getsize:
            bytes_used += os_path.getsize(os.path.join(path, obj_name))
            sleep()

    return object_count, bytes_used


def update_list(path, cont_path, dirs=[], files=[], object_count=0,
                bytes_used=0, obj_list=[]):
    if files:
        object_count, bytes_used = _update_list(path, cont_path, files, True,
                                                object_count, bytes_used,
                                                obj_list)
    if dirs:
        object_count, bytes_used = _update_list(path, cont_path, dirs, False,
                                                object_count, bytes_used,
                                                obj_list)
    return object_count, bytes_used


def get_container_details(cont_path):
    """
    get container details by traversing the filesystem
    """
    bytes_used = 0
    object_count = 0
    obj_list = []

    if os_path.isdir(cont_path):
        for (path, dirs, files) in do_walk(cont_path):
            object_count, bytes_used = update_list(path, cont_path, dirs,
                                                   files, object_count,
                                                   bytes_used, obj_list)

            sleep()

    return obj_list, object_count, bytes_used


def get_account_details(acc_path):
    """
    Return container_list and container_count.
    """
    container_list = []
    container_count = 0

    acc_stats = do_stat(acc_path)
    if acc_stats:
        is_dir = stat.S_ISDIR(acc_stats.st_mode)
        if is_dir:
            for name in do_listdir(acc_path):
                if name.lower() == TEMP_DIR \
                        or name.lower() == ASYNCDIR \
                        or not os_path.isdir(os.path.join(acc_path, name)):
                    continue
                container_count += 1
                container_list.append(name)

    return container_list, container_count


def _get_etag(path):
    """
    FIXME: It would be great to have a translator that returns the md5sum() of
    the file as an xattr that can be simply fetched.

    Since we don't have that we should yield after each chunk read and
    computed so that we don't consume the worker thread.
    """
    etag = md5()
    with open(path, 'rb') as fp:
        while True:
            chunk = fp.read(CHUNK_SIZE)
            if chunk:
                etag.update(chunk)
                if len(chunk) >= CHUNK_SIZE:
                    # It is likely that we have more data to be read from the
                    # file. Yield the co-routine cooperatively to avoid
                    # consuming the worker during md5sum() calculations on
                    # large files.
                    sleep()
            else:
                break
    return etag.hexdigest()


def get_object_metadata(obj_path):
    """
    Return metadata of object.
    """
    stats = do_stat(obj_path)
    if not stats:
        metadata = {}
    else:
        is_dir = stat.S_ISDIR(stats.st_mode)
        metadata = {
            X_TYPE: OBJECT,
            X_TIMESTAMP: normalize_timestamp(stats.st_ctime),
            X_CONTENT_TYPE: DIR_TYPE if is_dir else FILE_TYPE,
            X_OBJECT_TYPE: DIR_NON_OBJECT if is_dir else FILE,
            X_CONTENT_LENGTH: 0 if is_dir else stats.st_size,
            X_ETAG: md5().hexdigest() if is_dir else _get_etag(obj_path)}
    return metadata


def _add_timestamp(metadata_i):
    # At this point we have a simple key/value dictionary, turn it into
    # key/(value,timestamp) pairs.
    timestamp = 0
    metadata = {}
    for key, value_i in metadata_i.iteritems():
        if not isinstance(value_i, tuple):
            metadata[key] = (value_i, timestamp)
        else:
            metadata[key] = value_i
    return metadata


def get_container_metadata(cont_path):
    objects = []
    object_count = 0
    bytes_used = 0
    objects, object_count, bytes_used = get_container_details(cont_path)
    metadata = {X_TYPE: CONTAINER,
                X_TIMESTAMP: normalize_timestamp(
                    os_path.getctime(cont_path)),
                X_PUT_TIMESTAMP: normalize_timestamp(
                    os_path.getmtime(cont_path)),
                X_OBJECTS_COUNT: object_count,
                X_BYTES_USED: bytes_used}
    return _add_timestamp(metadata)


def get_account_metadata(acc_path):
    containers = []
    container_count = 0
    containers, container_count = get_account_details(acc_path)
    metadata = {X_TYPE: ACCOUNT,
                X_TIMESTAMP: normalize_timestamp(
                    os_path.getctime(acc_path)),
                X_PUT_TIMESTAMP: normalize_timestamp(
                    os_path.getmtime(acc_path)),
                X_OBJECTS_COUNT: 0,
                X_BYTES_USED: 0,
                X_CONTAINER_COUNT: container_count}
    return _add_timestamp(metadata)


def restore_metadata(path, metadata):
    meta_orig = read_metadata(path)
    if meta_orig:
        meta_new = meta_orig.copy()
        meta_new.update(metadata)
    else:
        meta_new = metadata
    if meta_orig != meta_new:
        write_metadata(path, meta_new)
    return meta_new


def create_object_metadata(obj_path):
    metadata = get_object_metadata(obj_path)
    return restore_metadata(obj_path, metadata)


def create_container_metadata(cont_path):
    metadata = get_container_metadata(cont_path)
    rmd = restore_metadata(cont_path, metadata)
    return rmd


def create_account_metadata(acc_path):
    metadata = get_account_metadata(acc_path)
    rmd = restore_metadata(acc_path, metadata)
    return rmd


def write_pickle(obj, dest, tmp=None, pickle_protocol=0):
    """
    Ensure that a pickle file gets written to disk.  The file is first written
    to a tmp file location in the destination directory path, ensured it is
    synced to disk, then moved to its final destination name.

    This version takes advantage of Gluster's dot-prefix-dot-suffix naming
    where the a file named ".thefile.name.9a7aasv" is hashed to the same
    Gluster node as "thefile.name". This ensures the renaming of a temp file
    once written does not move it to another Gluster node.

    :param obj: python object to be pickled
    :param dest: path of final destination file
    :param tmp: path to tmp to use, defaults to None (ignored)
    :param pickle_protocol: protocol to pickle the obj with, defaults to 0
    """
    dirname = os.path.dirname(dest)
    basename = os.path.basename(dest)
    tmpname = '.' + basename + '.' + \
        md5(basename + str(random.random())).hexdigest()
    tmppath = os.path.join(dirname, tmpname)
    with open(tmppath, 'wb') as fo:
        pickle.dump(obj, fo, pickle_protocol)
        # TODO: This flush() method call turns into a flush() system call
        # We'll need to wrap this as well, but we would do this by writing
        #a context manager for our own open() method which returns an object
        # in fo which makes the gluster API call.
        fo.flush()
        do_fsync(fo)
    do_rename(tmppath, dest)


# The following dir_xxx calls should definitely be replaced
# with a Metadata class to encapsulate their implementation.
# :FIXME: For now we have them as functions, but we should
# move them to a class.
def dir_is_object(metadata):
    """
    Determine if the directory with the path specified
    has been identified as an object
    """
    return metadata.get(X_OBJECT_TYPE, "") == DIR_OBJECT


def rmobjdir(dir_path):
    """
    Removes the directory as long as there are no objects stored in it. This
    works for containers also.
    """
    try:
        do_rmdir(dir_path)
    except OSError as err:
        if err.errno == errno.ENOENT:
            # No such directory exists
            return False
        if err.errno != errno.ENOTEMPTY:
            raise
        # Handle this non-empty directories below.
    else:
        return True

    # We have a directory that is not empty, walk it to see if it is filled
    # with empty sub-directories that are not user created objects
    # (gratuitously created as a result of other object creations).
    for (path, dirs, files) in do_walk(dir_path, topdown=False):
        for directory in dirs:
            fullpath = os.path.join(path, directory)

            try:
                metadata = read_metadata(fullpath)
            except OSError as err:
                if err.errno == errno.ENOENT:
                    # Ignore removal from another entity.
                    continue
                raise
            else:
                if dir_is_object(metadata):
                    # Wait, this is an object created by the caller
                    # We cannot delete
                    return False

            # Directory is not an object created by the caller
            # so we can go ahead and delete it.
            try:
                do_rmdir(fullpath)
            except OSError as err:
                if err.errno == errno.ENOTEMPTY:
                    # Directory is not empty, it might have objects in it
                    return False
                if err.errno == errno.ENOENT:
                    # No such directory exists, already removed, ignore
                    continue
                raise

    try:
        do_rmdir(dir_path)
    except OSError as err:
        if err.errno == errno.ENOTEMPTY:
            # Directory is not empty, race with object creation
            return False
        if err.errno == errno.ENOENT:
            # No such directory exists, already removed, ignore
            return True
        raise
    else:
        return True


# Over-ride Swift's utils.write_pickle with ours
#
# FIXME: Is this even invoked anymore given we don't perform container or
# account updates?
import swift.common.utils
swift.common.utils.write_pickle = write_pickle
