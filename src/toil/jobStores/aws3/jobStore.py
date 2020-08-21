# Copyright (C) 2015-2016 Regents of the University of California
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import

from future import standard_library

standard_library.install_aliases()
from builtins import str
from builtins import range
from contextlib import contextmanager, closing
import logging

try:
    import cPickle as pickle
except ImportError:
    import pickle

import re
import time
import uuid
import base64
import hashlib
import itertools
import reprlib
import urllib.parse
import urllib.request
import urllib.error
from io import BytesIO

from toil.lib.memoize import strict_bool
from toil.lib.exceptions import panic
from toil.lib.objects import InnerClass
import boto3
import boto.s3
import boto.sdb
from boto.exception import SDBResponseError, S3ResponseError
import botocore.session
import botocore.credentials
import botocore.exceptions

from toil.lib.compatibility import compat_bytes, compat_plain
from toil.lib.misc import AtomicFileCreate
from toil.fileStores import FileID
from toil.jobStores.abstractJobStore import (AbstractJobStore,
                                             NoSuchJobException,
                                             ConcurrentFileModificationException,
                                             NoSuchFileException,
                                             NoSuchJobStoreException,
                                             JobStoreExistsException)
from toil.jobStores.aws.utils import (SDBHelper,
                                      retry_sdb,
                                      no_such_sdb_domain,
                                      sdb_unavailable,
                                      monkeyPatchSdbConnection,
                                      retry_s3,
                                      retryable_s3_errors,
                                      bucket_location_to_region,
                                      region_to_bucket_location, copyKeyMultipart,
                                      chunkedFileUpload, fileSizeAndTime)
from toil.jobStores.aws3.utils import uploadFromPath

from toil.jobStores.utils import WritablePipe, ReadablePipe, ReadableTransformingPipe
from toil.jobGraph import JobGraph
import toil.lib.encryption as encryption

# Make sure to use credential caching when talking to Amazon via boto3
# See https://github.com/boto/botocore/pull/1338/
botocore_session = botocore.session.get_session()
botocore_session.get_component('credential_provider').get_provider(
    'assume-role').cache = botocore.credentials.JSONFileCache()
boto3_session = boto3.Session(botocore_session=botocore_session)
s3_boto3_resource = boto3_session.resource('s3')
s3_boto3_client = boto3_session.client('s3')
log = logging.getLogger(__name__)


class ChecksumError(Exception):
    """
    Raised when a download from AWS does not contain the correct data.
    """
    pass


class AWSJobStore(AbstractJobStore):
    """
    A job store that uses Amazon's S3 for file storage and SimpleDB for storing job info and
    enforcing strong consistency on the S3 file storage. There will be SDB domains for jobs and
    files and a versioned S3 bucket for file contents. Job objects are pickled, compressed,
    partitioned into chunks of 1024 bytes and each chunk is stored as a an attribute of the SDB
    item representing the job. UUIDs are used to identify jobs and files.
    """

    # Dots in bucket names should be avoided because bucket names are used in HTTPS bucket
    # URLs where the may interfere with the certificate common name. We use a double
    # underscore as a separator instead.
    #
    bucketNameRe = re.compile(r'^[a-z0-9][a-z0-9-]+[a-z0-9]$')

    # See http://docs.aws.amazon.com/AmazonS3/latest/dev/BucketRestrictions.html
    #
    minBucketNameLen = 3
    maxBucketNameLen = 63
    maxNameLen = 10
    nameSeparator = '--'

    def __init__(self, locator, partSize=50 << 20):
        """
        Create a new job store in AWS or load an existing one from there.

        :param int partSize: The size of each individual part used for multipart operations like
               upload and copy, must be >= 5 MiB but large enough to not exceed 10k parts for the
               whole file
        """
        super(AWSJobStore, self).__init__()
        log.warning(' ============================================================== ')
        log.warning('\tYou are using an experimental version of the AWS JobStore!!')
        log.warning(' ============================================================== ')

        region, namePrefix = locator.split(':')
        if not self.bucketNameRe.match(namePrefix):
            raise ValueError("Invalid name prefix '%s'. Name prefixes must contain only digits, "
                             "hyphens or lower-case letters and must not start or end in a "
                             "hyphen." % namePrefix)
        # Reserve 13 for separator and suffix
        if len(namePrefix) > self.maxBucketNameLen - self.maxNameLen - len(self.nameSeparator):
            raise ValueError("Invalid name prefix '%s'. Name prefixes may not be longer than 50 "
                             "characters." % namePrefix)
        if '--' in namePrefix:
            raise ValueError("Invalid name prefix '%s'. Name prefixes may not contain "
                             "%s." % (namePrefix, self.nameSeparator))
        log.debug("Instantiating %s for region %s and name prefix '%s'",
                  self.__class__, region, namePrefix)
        self.locator = locator
        self.region = region
        self.namePrefix = namePrefix
        self.partSize = partSize
        self.jobsDomain = None
        self.filesDomain = None
        self.filesBucket = None
        self.boto3FilesBucket = None  # to be renamed to filesBucket
        self.s3_client = None
        self.s3_resource = None
        self.db = self._connectSimpleDB()
        self.s3 = self._connectS3()

    def initialize(self, config):
        if self._registered:
            raise JobStoreExistsException(self.locator)
        self._registered = None
        try:
            self._bind(create=True)
        except:
            with panic(log):
                self.destroy()
        else:
            super(AWSJobStore, self).initialize(config)
            # Only register after job store has been full initialized
            self._registered = True

    @property
    def sseKeyPath(self):
        return self.config.sseKey

    def resume(self):
        if not self._registered:
            raise NoSuchJobStoreException(self.locator)
        self._bind(create=False)
        super(AWSJobStore, self).resume()

    def _bind(self, create=False, block=True, check_versioning_consistency=True):
        def qualify(name):
            assert len(name) <= self.maxNameLen
            return self.namePrefix + self.nameSeparator + name

        # The order in which this sequence of events happens is important.  We can easily handle the
        # inability to bind a domain, but it is a little harder to handle some cases of binding the
        # jobstore bucket.  Maintaining this order allows for an easier `destroy` method.
        if self.jobsDomain is None:
            self.jobsDomain = self._bindDomain(qualify('jobs'), create=create, block=block)
        if self.filesDomain is None:
            self.filesDomain = self._bindDomain(qualify('files'), create=create, block=block)
        if self.filesBucket is None:
            self.filesBucket = self._bindBucket(qualify('files'),
                                                create=create,
                                                block=block,
                                                versioning=True,
                                                check_versioning_consistency=check_versioning_consistency)

    @property
    def _registered(self):
        """
        A optional boolean property indidcating whether this job store is registered. The
        registry is the authority on deciding if a job store exists or not. If True, this job
        store exists, if None the job store is transitioning from True to False or vice versa,
        if False the job store doesn't exist.

        :type: bool|None
        """
        # The weird mapping of the SDB item attribute value to the property value is due to
        # backwards compatibility. 'True' becomes True, that's easy. Toil < 3.3.0 writes this at
        # the end of job store creation. Absence of either the registry, the item or the
        # attribute becomes False, representing a truly absent, non-existing job store. An
        # attribute value of 'False', which is what Toil < 3.3.0 writes at the *beginning* of job
        # store destruction, indicates a job store in transition, reflecting the fact that 3.3.0
        # may leak buckets or domains even though the registry reports 'False' for them. We
        # can't handle job stores that were partially created by 3.3.0, though.
        registry_domain = self._bindDomain(domain_name='toil-registry',
                                           create=False,
                                           block=False)
        if registry_domain is None:
            return False
        else:
            for attempt in retry_sdb():
                with attempt:
                    attributes = registry_domain.get_attributes(item_name=self.namePrefix,
                                                                attribute_name='exists',
                                                                consistent_read=True)
                    try:
                        exists = attributes['exists']
                    except KeyError:
                        return False
                    else:
                        if exists == 'True':
                            return True
                        elif exists == 'False':
                            return None
                        else:
                            assert False

    @_registered.setter
    def _registered(self, value):

        registry_domain = self._bindDomain(domain_name='toil-registry',
                                           # Only create registry domain when registering or
                                           # transitioning a store
                                           create=value is not False,
                                           block=False)
        if registry_domain is None and value is False:
            pass
        else:
            for attempt in retry_sdb():
                with attempt:
                    if value is False:
                        registry_domain.delete_attributes(item_name=self.namePrefix)
                    else:
                        if value is True:
                            attributes = dict(exists='True')
                        elif value is None:
                            attributes = dict(exists='False')
                        else:
                            assert False
                        registry_domain.put_attributes(item_name=self.namePrefix,
                                                       attributes=attributes)

    def _checkItem(self, item):
        if "overlargeID" not in item:
            raise RuntimeError("overlargeID attribute isn't present: you are restarting"
                               " an old, incompatible jobstore, or the jobstore logic is"
                               " incorrect.")

    def _awsJobFromItem(self, item):
        self._checkItem(item)
        if item["overlargeID"]:
            assert self.fileExists(item["overlargeID"])
            # This is an overlarge job, download the actual attributes
            # from the file store
            log.debug("Loading overlarge job from S3.")
            with self.readFileStream(item["overlargeID"]) as fh:
                binary = fh.read()
        else:
            binary, _ = SDBHelper.attributesToBinary(item)
            assert binary is not None
        job = pickle.loads(binary)
        return job

    def _awsJobToItem(self, job):
        binary = pickle.dumps(job, protocol=pickle.HIGHEST_PROTOCOL)
        if len(binary) > SDBHelper.maxBinarySize(extraReservedChunks=1):
            # Store as an overlarge job in S3
            with self.writeFileStream() as (writable, fileID):
                writable.write(binary)
            item = SDBHelper.binaryToAttributes(None)
            item["overlargeID"] = fileID
        else:
            item = SDBHelper.binaryToAttributes(binary)
            item["overlargeID"] = ""
        return item

    jobsPerBatchInsert = 25

    @contextmanager
    def batch(self):
        self._batchedJobGraphs = []
        yield
        batches = [self._batchedJobGraphs[i:i + self.jobsPerBatchInsert] for i in
                   range(0, len(self._batchedJobGraphs), self.jobsPerBatchInsert)]

        for batch in batches:
            items = {jobGraph.jobStoreID: self._awsJobToItem(jobGraph) for jobGraph in batch}
            for attempt in retry_sdb():
                with attempt:
                    assert self.jobsDomain.batch_put_attributes(items)
        self._batchedJobGraphs = None

    def create(self, jobNode):
        jobStoreID = self._newJobID()
        log.debug("Creating job %s for '%s'",
                  jobStoreID, '<no command>' if jobNode.command is None else jobNode.command)
        job = JobGraph.fromJobNode(jobNode, jobStoreID=jobStoreID, tryCount=self._defaultTryCount())

        if hasattr(self, "_batchedJobGraphs") and self._batchedJobGraphs is not None:
            self._batchedJobGraphs.append(job)
        else:
            item = self._awsJobToItem(job)
            for attempt in retry_sdb():
                with attempt:
                    assert self.jobsDomain.put_attributes(job.jobStoreID, item)
        return job

    def exists(self, jobStoreID):
        for attempt in retry_sdb():
            with attempt:
                return bool(self.jobsDomain.get_attributes(
                    item_name=compat_bytes(jobStoreID),
                    attribute_name=[SDBHelper.presenceIndicator()],
                    consistent_read=True))

    def jobs(self):
        result = None
        for attempt in retry_sdb():
            with attempt:
                result = list(self.jobsDomain.select(
                    consistent_read=True,
                    query="select * from `%s`" % self.jobsDomain.name))
        assert result is not None
        for jobItem in result:
            yield self._awsJobFromItem(jobItem)

    def load(self, jobStoreID):
        item = None
        for attempt in retry_sdb():
            with attempt:
                item = self.jobsDomain.get_attributes(compat_bytes(jobStoreID), consistent_read=True)
        if not item:
            raise NoSuchJobException(jobStoreID)
        job = self._awsJobFromItem(item)
        if job is None:
            raise NoSuchJobException(jobStoreID)
        log.debug("Loaded job %s", jobStoreID)
        return job

    def update(self, job):
        log.debug("Updating job %s", job.jobStoreID)
        item = self._awsJobToItem(job)
        for attempt in retry_sdb():
            with attempt:
                assert self.jobsDomain.put_attributes(compat_bytes(job.jobStoreID), item)

    itemsPerBatchDelete = 25

    def delete(self, jobStoreID):
        # remove job and replace with jobStoreId.
        log.debug("Deleting job %s", jobStoreID)

        # If the job is overlarge, delete its file from the filestore
        item = None
        for attempt in retry_sdb():
            with attempt:
                item = self.jobsDomain.get_attributes(compat_bytes(jobStoreID), consistent_read=True)
        self._checkItem(item)
        if item["overlargeID"]:
            log.debug("Deleting job from filestore")
            self.deleteFile(item["overlargeID"])
        for attempt in retry_sdb():
            with attempt:
                self.jobsDomain.delete_attributes(item_name=compat_bytes(jobStoreID))
        items = None
        for attempt in retry_sdb():
            with attempt:
                items = list(self.filesDomain.select(
                    consistent_read=True,
                    query="select version from `%s` where ownerID='%s'" % (
                        self.filesDomain.name, jobStoreID)))
        assert items is not None
        if items:
            log.debug("Deleting %d file(s) associated with job %s", len(items), jobStoreID)
            n = self.itemsPerBatchDelete
            batches = [items[i:i + n] for i in range(0, len(items), n)]
            for batch in batches:
                itemsDict = {item.name: None for item in batch}
                for attempt in retry_sdb():
                    with attempt:
                        self.filesDomain.batch_delete_attributes(itemsDict)
            for item in items:
                version = item.get('version')
                for attempt in retry_s3():
                    with attempt:
                        if version:
                            self.filesBucket.delete_key(key_name=compat_bytes(item.name), version_id=version)
                        else:
                            self.filesBucket.delete_key(key_name=compat_bytes(item.name))

    def getEmptyFileStoreID(self, jobStoreID=None, cleanup=False, basename=None):
        info = self.FileInfo.create(jobStoreID if cleanup else None)
        with info.uploadStream() as _:
            # Empty
            pass
        info.save()
        log.debug("Created %r.", info)
        return info.fileID

    def _importFile(self, otherCls, url, sharedFileName=None, hardlink=False):
        if issubclass(otherCls, AWSJobStore):
            srcKey = self._getKeyForUrl(url, existing=True)
            size = srcKey.size
            try:
                if sharedFileName is None:
                    info = self.FileInfo.create(srcKey.name)
                else:
                    self._requireValidSharedFileName(sharedFileName)
                    jobStoreFileID = self._sharedFileID(sharedFileName)
                    info = self.FileInfo.loadOrCreate(jobStoreFileID=jobStoreFileID,
                                                      ownerID=str(self.sharedFileOwnerID),
                                                      encrypted=None)
                info.copyFrom(srcKey)
                info.save()
            finally:
                srcKey.bucket.connection.close()
            return FileID(info.fileID, size) if sharedFileName is None else None
        else:
            return super(AWSJobStore, self)._importFile(otherCls, url,
                                                        sharedFileName=sharedFileName)

    def _exportFile(self, otherCls, jobStoreFileID, url):
        if issubclass(otherCls, AWSJobStore):
            dstKey = self._getKeyForUrl(url)
            try:
                info = self.FileInfo.loadOrFail(jobStoreFileID)
                info.copyTo(dstKey)
            finally:
                dstKey.bucket.connection.close()
        else:
            super(AWSJobStore, self)._defaultExportFile(otherCls, jobStoreFileID, url)

    @classmethod
    def getSize(cls, url):
        # [!!] TODO: needs testing
        return cls._getObjectForUrl(url, existing=True).size

    @classmethod
    def _readFromUrl(cls, url, writable):
        # [!!] TODO: needs testing
        srcObj = cls._getObjectForUrl(url, existing=True)
        srcObj.download_fileobj(writable)
        return srcObj.size

    @classmethod
    def _writeToUrl(cls, readable, url):
        # [!!] TODO: needs testing
        dstObj = cls._getObjectForUrl(url)
        canDetermineSize = True
        fileSize = 0
        try:
            readable.seek(0, 2)  # go to the 0th byte from the end of the file, indicated by '2'
            fileSize = readable.tell()  # tells the current position in file - in this case == size of file
            readable.seek(0)  # go to the 0th byte from the start of the file
        except:
            canDetermineSize = False
        if canDetermineSize and fileSize > (5 * 1000 * 1000):  # only use multipart when file is above 5 mb
            log.debug("Uploading %s with size %s, will use multipart uploading", dstObj.key, fileSize)
            # chunkedFileUpload(readable=readable, bucket=dstObj.Bucket(), fileID=dstObj.key, file_size=fileSize)
            raise NotImplementedError()
        else:
            # we either don't know the size, or the size is small
            log.debug("Can not use multipart uploading for %s, uploading whole file at once", dstObj.key)
            dstObj.put(Body=readable.read())

    @staticmethod
    def _getKeyForUrl(url, existing=None):
        """
        Deprecated by toil.jobStores.aws3.AWSJobStore._getObjectForUrl.

        Extracts a key from a given s3:// URL. On return, but not on exceptions, this method
        leaks an S3Connection object. The caller is responsible to close that by calling
        key.bucket.connection.close().

        :param bool existing: If True, key is expected to exist. If False, key is expected not to
               exists and it will be created. If None, the key will be created if it doesn't exist.

        :rtype: Key
        """
        keyName = url.path[1:]
        bucketName = url.netloc

        # Get the bucket's region to avoid a redirect per request
        try:
            with closing(boto.connect_s3()) as s3:
                location = s3.get_bucket(bucketName).get_location()
                region = bucket_location_to_region(location)
        except S3ResponseError as e:
            if e.error_code == 'AccessDenied':
                s3 = boto.connect_s3()
            else:
                raise
        else:
            # Note that caller is responsible for closing the connection
            s3 = boto.s3.connect_to_region(region)

        try:
            bucket = s3.get_bucket(bucketName)
            key = bucket.get_key(keyName.encode('utf-8'))
            if existing is True:
                if key is None:
                    raise RuntimeError("Key '%s' does not exist in bucket '%s'." %
                                       (keyName, bucketName))
            elif existing is False:
                if key is not None:
                    raise RuntimeError("Key '%s' exists in bucket '%s'." %
                                       (keyName, bucketName))
            elif existing is None:
                pass
            else:
                assert False
            if key is None:
                key = bucket.new_key(keyName)
        except:
            with panic():
                s3.close()
        else:
            return key

    @staticmethod
    def _getObjectForUrl(url, existing=None):
        """
        Extracts a key (object) from a given s3:// URL.

        :param bool existing: If True, key is expected to exist. If False, key is expected not to
        exists and it will be created. If None, the key will be created if it doesn't exist.

        :rtype: S3KeyWrapper
        """
        # In boto3, S3.Object represents an S3 Object, whereas
        # in boto2, boto.s3.key.Key represents a key (object) in an S3 bucket
        # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3.html#object
        # http://boto.cloudhackers.com/en/latest/ref/s3.html#module-boto.s3.key
        keyName = url.path[1:]
        bucketName = url.netloc

        # Get the bucket's region to avoid a redirect per request
        region = AWSJobStore.__getBucketRegion(bucketName)

        s3 = boto3_session.resource('s3', region_name=region)
        obj = s3.Object(bucketName, keyName)
        objExists = True
        try:
            obj.load()
        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] == '404':
                objExists = False
            else:
                raise
        if existing is True:
            if not objExists:
                raise RuntimeError("Key '%s' does not exist in bucket '%s'." % (keyName, bucketName))
        elif existing is False:
            if objExists:
                raise RuntimeError("Key '%s' exists in bucket '%s'." % (keyName, bucketName))
        elif existing is None:
            pass
        else:
            assert False
        if not objExists:
            obj.put()  # write an empty file
        # return obj
        # Use a wrapper until all usages are converted to S3.Object syntax
        from toil.jobStores.aws3.utils import S3KeyWrapper
        return S3KeyWrapper(obj)

    @classmethod
    def _supportsUrl(cls, url, export=False):
        return url.scheme.lower() == 's3'

    def writeFile(self, localFilePath, jobStoreID=None, cleanup=False):
        info = self.FileInfo.create(jobStoreID if cleanup else None)
        info.upload(localFilePath, not self.config.disableJobStoreChecksumVerification)
        info.save()
        log.debug("Wrote %r of from %r", info, localFilePath)
        return info.fileID

    @contextmanager
    def writeFileStream(self, jobStoreID=None, cleanup=False, basename=None):
        info = self.FileInfo.create(jobStoreID if cleanup else None)
        with info.uploadStream() as writable:
            yield writable, info.fileID
        info.save()
        log.debug("Wrote %r.", info)

    @contextmanager
    def writeSharedFileStream(self, sharedFileName, isProtected=None):
        self._requireValidSharedFileName(sharedFileName)
        info = self.FileInfo.loadOrCreate(jobStoreFileID=self._sharedFileID(sharedFileName),
                                          ownerID=str(self.sharedFileOwnerID),
                                          encrypted=isProtected)
        with info.uploadStream() as writable:
            yield writable
        info.save()
        log.debug("Wrote %r for shared file %r.", info, sharedFileName)

    def updateFile(self, jobStoreFileID, localFilePath):
        info = self.FileInfo.loadOrFail(jobStoreFileID)
        info.upload(localFilePath, not self.config.disableJobStoreChecksumVerification)
        info.save()
        log.debug("Wrote %r from path %r.", info, localFilePath)

    @contextmanager
    def updateFileStream(self, jobStoreFileID):
        info = self.FileInfo.loadOrFail(jobStoreFileID)
        with info.uploadStream() as writable:
            yield writable
        info.save()
        log.debug("Wrote %r from stream.", info)

    def fileExists(self, jobStoreFileID):
        return self.FileInfo.exists(jobStoreFileID)

    def getFileSize(self, jobStoreFileID):
        if not self.fileExists(jobStoreFileID):
            return 0
        info = self.FileInfo.loadOrFail(jobStoreFileID)
        return info.getSize()

    def readFile(self, jobStoreFileID, localFilePath, symlink=False):
        info = self.FileInfo.loadOrFail(jobStoreFileID)
        log.debug("Reading %r into %r.", info, localFilePath)
        info.download(localFilePath, not self.config.disableJobStoreChecksumVerification)

    @contextmanager
    def readFileStream(self, jobStoreFileID):
        info = self.FileInfo.loadOrFail(jobStoreFileID)
        log.debug("Reading %r into stream.", info)
        with info.downloadStream() as readable:
            yield readable

    @contextmanager
    def readSharedFileStream(self, sharedFileName):
        self._requireValidSharedFileName(sharedFileName)
        jobStoreFileID = self._sharedFileID(sharedFileName)
        info = self.FileInfo.loadOrFail(jobStoreFileID, customName=sharedFileName)
        log.debug("Reading %r for shared file %r into stream.", info, sharedFileName)
        with info.downloadStream() as readable:
            yield readable

    def deleteFile(self, jobStoreFileID):
        info = self.FileInfo.load(jobStoreFileID)
        if info is None:
            log.debug("File %s does not exist, skipping deletion.", jobStoreFileID)
        else:
            info.delete()

    def writeStatsAndLogging(self, statsAndLoggingString):
        info = self.FileInfo.create(str(self.statsFileOwnerID))
        with info.uploadStream(multipart=False) as writeable:
            if isinstance(statsAndLoggingString, str):
                # This stream is for binary data, so encode any non-encoded things
                statsAndLoggingString = statsAndLoggingString.encode('utf-8', errors='ignore')
            writeable.write(statsAndLoggingString)
        info.save()

    def readStatsAndLogging(self, callback, readAll=False):
        itemsProcessed = 0

        for info in self._readStatsAndLogging(callback, self.statsFileOwnerID):
            info._ownerID = self.readStatsFileOwnerID
            info.save()
            itemsProcessed += 1

        if readAll:
            for _ in self._readStatsAndLogging(callback, self.readStatsFileOwnerID):
                itemsProcessed += 1

        return itemsProcessed

    def _readStatsAndLogging(self, callback, ownerId):
        items = None
        for attempt in retry_sdb():
            with attempt:
                items = list(self.filesDomain.select(
                    consistent_read=True,
                    query="select * from `%s` where ownerID='%s'" % (
                        self.filesDomain.name, str(ownerId))))
        assert items is not None
        for item in items:
            info = self.FileInfo.fromItem(item)
            with info.downloadStream() as readable:
                callback(readable)
            yield info

    def getPublicUrl(self, jobStoreFileID):
        info = self.FileInfo.loadOrFail(jobStoreFileID)
        if info.content is not None:
            with info.uploadStream(allowInlining=False) as f:
                f.write(info.content)
        for attempt in retry_s3():
            with attempt:
                key = self.filesBucket.get_key(key_name=compat_bytes(jobStoreFileID), version_id=info.version)
                key.set_canned_acl('public-read')
                url = key.generate_url(query_auth=False,
                                       expires_in=self.publicUrlExpiration.total_seconds())
                # boto doesn't properly remove the x-amz-security-token parameter when
                # query_auth is False when using an IAM role (see issue #2043). Including the
                # x-amz-security-token parameter without the access key results in a 403,
                # even if the resource is public, so we need to remove it.
                scheme, netloc, path, query, fragment = urllib.parse.urlsplit(url)
                params = urllib.parse.parse_qs(query)
                if 'x-amz-security-token' in params:
                    del params['x-amz-security-token']
                query = urllib.parse.urlencode(params, doseq=True)
                url = urllib.parse.urlunsplit((scheme, netloc, path, query, fragment))
                return url

    def getSharedPublicUrl(self, sharedFileName):
        self._requireValidSharedFileName(sharedFileName)
        return self.getPublicUrl(self._sharedFileID(sharedFileName))

    def _connectSimpleDB(self):
        """
        :rtype: SDBConnection
        """
        db = boto.sdb.connect_to_region(self.region)
        if db is None:
            raise ValueError("Could not connect to SimpleDB. Make sure '%s' is a valid SimpleDB region." % self.region)
        monkeyPatchSdbConnection(db)
        return db

    def _connectS3(self):
        """
        :rtype: S3Connection
        """
        self.s3_client = boto3_session.client('s3', region_name=self.region)
        self.s3_resource = boto3_session.resource('s3', region_name=self.region)

        s3 = boto.s3.connect_to_region(self.region)
        if s3 is None:
            raise ValueError("Could not connect to S3. Make sure '%s' is a valid S3 region." % self.region)
        return s3

    def _bindBucket(self, bucket_name, create=False, block=True, versioning=False,
                    check_versioning_consistency=True):
        """
        Return the Boto Bucket object representing the S3 bucket with the given name. If the
        bucket does not exist and `create` is True, it will be created.

        :param str bucket_name: the name of the bucket to bind to

        :param bool create: Whether to create bucket the if it doesn't exist

        :param bool block: If False, return None if the bucket doesn't exist. If True, wait until
               bucket appears. Ignored if `create` is True.

        :rtype: Bucket|None
        :raises botocore.exceptions.ClientError: If `block` is True and the bucket still doesn't exist after the
                retry timeout expires.
        """
        assert self.minBucketNameLen <= len(bucket_name) <= self.maxBucketNameLen
        assert self.bucketNameRe.match(bucket_name)
        log.debug("Binding to job store bucket '%s'.", bucket_name)

        def bucket_creation_pending(e):
            # https://github.com/BD2KGenomics/toil/issues/955
            # https://github.com/BD2KGenomics/toil/issues/995
            # https://github.com/BD2KGenomics/toil/issues/1093
            return (isinstance(e, botocore.exceptions.ClientError)
                    and e.response['Error']['Code'] in ('BucketAlreadyOwnedByYou',
                                                        'OperationAborted',
                                                        '404',  # TODO: verify?
                                                        'NoSuchBucket'))

        bucketExisted = True
        for attempt in retry_s3(predicate=bucket_creation_pending):
            with attempt:
                # migration from boto2
                # see https://boto3.amazonaws.com/v1/documentation/api/latest/guide/migrations3.html#accessing-a-bucket
                try:
                    self.s3_resource.meta.client.head_bucket(Bucket=bucket_name)
                except botocore.exceptions.ClientError as e:
                    errorCode = e.response['Error']['Code']
                    if errorCode == '404' or errorCode == 'NoSuchBucket':
                        bucketExisted = False
                        log.debug("Bucket '%s' does not exist.", bucket_name)
                        if create:
                            log.debug("Creating bucket '%s'.", bucket_name)
                            location = region_to_bucket_location(self.region)
                            self.s3_resource.create_bucket(
                                Bucket=bucket_name,
                                CreateBucketConfiguration={'LocationConstraint': location})
                            # It is possible for create_bucket to return but
                            # for an immediate request for the bucket region to
                            # produce an S3ResponseError with code
                            # NoSuchBucket. We let that kick us back up to the
                            # main retry loop.
                            assert self.__getBucketRegion(bucket_name) == self.region
                        elif block:
                            raise
                        else:
                            return None
                    elif errorCode == '301':
                        # TODO: not yet able to reproduce
                        # This is raised if the user attempts to get a bucket in a region outside
                        # the specified one, if the specified one is not `us-east-1`.  The us-east-1
                        # server allows a user to use buckets from any region.
                        raise BucketLocationConflictException(self.__getBucketRegion(bucket_name))
                    else:
                        raise
                else:
                    bucketRegion = self.__getBucketRegion(bucket_name)
                    if bucketRegion != self.region:
                        raise BucketLocationConflictException(bucketRegion)

                bucket = self.s3_resource.Bucket(bucket_name)

                if versioning and not bucketExisted:
                    # only call this method on bucket creation
                    # https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3.html#bucketversioning
                    bucket.Versioning().enable()
                    # Now wait until versioning is actually on. Some uploads
                    # would come back with no versions; maybe they were
                    # happening too fast and this setting isn't sufficiently
                    # consistent?
                    time.sleep(1)
                    while not self._getBucketVersioningFromName(bucket_name):
                        log.warning("Waiting for versioning activation on bucket '%s'...", bucket_name)
                        time.sleep(1)
                elif check_versioning_consistency:
                    # now test for versioning consistency
                    # we should never see any of these errors since 'versioning' should always be true
                    bucket_versioning = self._getBucketVersioningFromName(bucket_name)
                    if bucket_versioning != versioning:
                        assert False, 'Cannot modify versioning on existing bucket'
                    elif bucket_versioning is None:
                        assert False, 'Cannot use a bucket with versioning suspended'
                if bucketExisted:
                    log.debug("Using pre-existing job store bucket '%s'.", bucket_name)
                else:
                    log.debug("Created new job store bucket '%s' with versioning state %s.", bucket_name,
                              str(versioning))

                # return bucket
                # In the meantime still return the Bucket instance from boto2
                self.boto3FilesBucket = bucket
                return self.s3.get_bucket(bucket_name, validate=True)

    def _bindDomain(self, domain_name, create=False, block=True):
        """
        Return the Boto Domain object representing the SDB domain of the given name. If the
        domain does not exist and `create` is True, it will be created.

        :param str domain_name: the name of the domain to bind to

        :param bool create: True if domain should be created if it doesn't exist

        :param bool block: If False, return None if the domain doesn't exist. If True, wait until
               domain appears. This parameter is ignored if create is True.

        :rtype: Domain|None
        :raises SDBResponseError: If `block` is True and the domain still doesn't exist after the
                retry timeout expires.
        """
        log.debug("Binding to job store domain '%s'.", domain_name)
        retryargs = dict(predicate=lambda e: no_such_sdb_domain(e) or sdb_unavailable(e))
        if not block:
            retryargs['timeout'] = 15
        for attempt in retry_sdb(**retryargs):
            with attempt:
                try:
                    return self.db.get_domain(domain_name)
                except SDBResponseError as e:
                    if no_such_sdb_domain(e):
                        if create:
                            return self.db.create_domain(domain_name)
                        elif block:
                            raise
                        else:
                            return None
                    else:
                        raise

    def _newJobID(self):
        return str(uuid.uuid4())

    # A dummy job ID under which all shared files are stored
    sharedFileOwnerID = uuid.UUID('891f7db6-e4d9-4221-a58e-ab6cc4395f94')

    # A dummy job ID under which all unread stats files are stored
    statsFileOwnerID = uuid.UUID('bfcf5286-4bc7-41ef-a85d-9ab415b69d53')

    # A dummy job ID under which all read stats files are stored
    readStatsFileOwnerID = uuid.UUID('e77fc3aa-d232-4255-ae04-f64ee8eb0bfa')

    def _sharedFileID(self, sharedFileName):
        return str(uuid.uuid5(self.sharedFileOwnerID, sharedFileName))

    @InnerClass
    class FileInfo(SDBHelper):
        """
        Represents a file in this job store.
        """
        outer = None
        """
        :type: AWSJobStore
        """

        def __init__(self, fileID, ownerID, encrypted,
                     version=None, content=None, numContentChunks=0, checksum=None):
            """
            :type fileID: str
            :param fileID: the file's ID

            :type ownerID: str
            :param ownerID: ID of the entity owning this file, typically a job ID aka jobStoreID

            :type encrypted: bool
            :param encrypted: whether the file is stored in encrypted form

            :type version: str|None
            :param version: a non-empty string containing the most recent version of the S3
            object storing this file's content, None if the file is new, or empty string if the
            file is inlined.

            :type content: str|None
            :param content: this file's inlined content

            :type numContentChunks: int
            :param numContentChunks: the number of SDB domain attributes occupied by this files

            :type checksum: str|None
            :param checksum: the checksum of the file, if available. Formatted
            as <algorithm>$<lowercase hex hash>.

            inlined content. Note that an inlined empty string still occupies one chunk.
            """
            super(AWSJobStore.FileInfo, self).__init__()
            self._fileID = fileID
            self._ownerID = ownerID
            self.encrypted = encrypted
            self._version = version
            self._previousVersion = version
            self._content = content
            self._checksum = checksum
            self._numContentChunks = numContentChunks

        @property
        def fileID(self):
            return self._fileID

        @property
        def ownerID(self):
            return self._ownerID

        @property
        def version(self):
            return self._version

        @version.setter
        def version(self, version):
            # Version should only change once
            assert self._previousVersion == self._version
            self._version = version
            if version:
                self.content = None

        @property
        def previousVersion(self):
            return self._previousVersion

        @property
        def content(self):
            return self._content

        @property
        def checksum(self):
            return self._checksum

        @checksum.setter
        def checksum(self, checksum):
            self._checksum = checksum

        @content.setter
        def content(self, content):
            assert content is None or isinstance(content, bytes)
            self._content = content
            if content is not None:
                self.version = ''

        @classmethod
        def create(cls, ownerID):
            return cls(str(uuid.uuid4()), ownerID, encrypted=cls.outer.sseKeyPath is not None)

        @classmethod
        def presenceIndicator(cls):
            return 'encrypted'

        @classmethod
        def exists(cls, jobStoreFileID):
            for attempt in retry_sdb():
                with attempt:
                    return bool(cls.outer.filesDomain.get_attributes(
                        item_name=compat_bytes(jobStoreFileID),
                        attribute_name=[cls.presenceIndicator()],
                        consistent_read=True))

        @classmethod
        def load(cls, jobStoreFileID):
            for attempt in retry_sdb():
                with attempt:
                    self = cls.fromItem(
                        cls.outer.filesDomain.get_attributes(item_name=compat_bytes(jobStoreFileID),
                                                             consistent_read=True))
                    return self

        @classmethod
        def loadOrCreate(cls, jobStoreFileID, ownerID, encrypted):
            self = cls.load(jobStoreFileID)
            if encrypted is None:
                encrypted = cls.outer.sseKeyPath is not None
            if self is None:
                self = cls(jobStoreFileID, ownerID, encrypted=encrypted)
            else:
                assert self.fileID == jobStoreFileID
                assert self.ownerID == ownerID
                self.encrypted = encrypted
            return self

        @classmethod
        def loadOrFail(cls, jobStoreFileID, customName=None):
            """
            :rtype: AWSJobStore.FileInfo
            :return: an instance of this class representing the file with the given ID
            :raises NoSuchFileException: if given file does not exist
            """
            self = cls.load(jobStoreFileID)
            if self is None:
                raise NoSuchFileException(jobStoreFileID, customName=customName)
            else:
                return self

        @classmethod
        def fromItem(cls, item):
            """
            Convert an SDB item to an instance of this class.

            :type item: Item
            """
            assert item is not None

            # Strings come back from SDB as unicode
            def strOrNone(s):
                return s if s is None else str(s)

            # ownerID and encrypted are the only mandatory attributes
            ownerID = strOrNone(item.get('ownerID'))
            encrypted = item.get('encrypted')
            if ownerID is None:
                assert encrypted is None
                return None
            else:
                version = strOrNone(item['version'])
                checksum = strOrNone(item.get('checksum'))
                encrypted = strict_bool(encrypted)
                content, numContentChunks = cls.attributesToBinary(item)
                if encrypted:
                    sseKeyPath = cls.outer.sseKeyPath
                    if sseKeyPath is None:
                        raise AssertionError('Content is encrypted but no key was provided.')
                    if content is not None:
                        content = encryption.decrypt(content, sseKeyPath)
                self = cls(fileID=item.name, ownerID=ownerID, encrypted=encrypted, version=version,
                           content=content, numContentChunks=numContentChunks, checksum=checksum)
                return self

        def toItem(self):
            """
            Convert this instance to an attribute dictionary suitable for SDB put_attributes().

            :rtype: (dict,int)

            :return: the attributes dict and an integer specifying the the number of chunk
                     attributes in the dictionary that are used for storing inlined content.
            """
            content = self.content
            assert content is None or isinstance(content, bytes)
            if self.encrypted and content is not None:
                sseKeyPath = self.outer.sseKeyPath
                if sseKeyPath is None:
                    raise AssertionError('Encryption requested but no key was provided.')
                content = encryption.encrypt(content, sseKeyPath)
            assert content is None or isinstance(content, bytes)
            attributes = self.binaryToAttributes(content)
            numChunks = attributes['numChunks']
            attributes.update(dict(ownerID=self.ownerID,
                                   encrypted=self.encrypted,
                                   version=self.version or '',
                                   checksum=self.checksum or ''))
            return attributes, numChunks

        @classmethod
        def _reservedAttributes(cls):
            return 3 + super(AWSJobStore.FileInfo, cls)._reservedAttributes()

        @staticmethod
        def maxInlinedSize():
            return 256

        def save(self):
            attributes, numNewContentChunks = self.toItem()
            # False stands for absence
            expected = ['version', False if self.previousVersion is None else self.previousVersion]
            try:
                for attempt in retry_sdb():
                    with attempt:
                        assert self.outer.filesDomain.put_attributes(item_name=compat_bytes(self.fileID),
                                                                     attributes=attributes,
                                                                     expected_value=expected)
                # clean up the old version of the file if necessary and safe
                if self.previousVersion and (self.previousVersion != self.version):
                    for attempt in retry_s3():
                        with attempt:
                            self.outer.s3_client.delete_object(Bucket=self.outer.boto3FilesBucket.name,
                                                               Key=compat_bytes(self.fileID),
                                                               VersionId=self.previousVersion)
                self._previousVersion = self._version
                if numNewContentChunks < self._numContentChunks:
                    residualChunks = range(numNewContentChunks, self._numContentChunks)
                    attributes = [self._chunkName(i) for i in residualChunks]
                    for attempt in retry_sdb():
                        with attempt:
                            self.outer.filesDomain.delete_attributes(compat_bytes(self.fileID),
                                                                     attributes=attributes)
                self._numContentChunks = numNewContentChunks
            except SDBResponseError as e:
                if e.error_code == 'ConditionalCheckFailed':
                    raise ConcurrentFileModificationException(self.fileID)
                else:
                    raise

        def upload(self, localFilePath, calculateChecksum=True):
            file_size, file_time = fileSizeAndTime(localFilePath)
            if file_size <= self.maxInlinedSize():
                with open(localFilePath, 'rb') as f:
                    self.content = f.read()
                # Clear out any old checksum in case of overwrite
                self.checksum = ''
            else:
                headers = self._s3EncryptionHeaders()
                self.checksum = self._get_file_checksum(localFilePath) if calculateChecksum else None
                # self.version = uploadFromPath(localFilePath, partSize=self.outer.partSize,
                #                               bucket=self.outer.filesBucket, fileID=compat_bytes(self.fileID),
                #                               headers=headers)
                # TODO: needs testing. This is using the aws3/utils implementation
                self.version = uploadFromPath(localFilePath, partSize=self.outer.partSize,
                                              bucket=self.outer.boto3FilesBucket, fileID=compat_bytes(self.fileID),
                                              headers=headers)

        def _start_checksum(self, to_match=None, algorithm='sha1'):
            """
            Get a hasher that can be used with _update_checksum and
            _finish_checksum.

            If to_match is set, it is a precomputed checksum which we expect
            the result to match.

            The right way to compare checksums is to feed in the checksum to be
            matched, so we can see its algorithm, instead of getting a new one
            and comparing. If a checksum to match is fed in, _finish_checksum()
            will raise a ChecksumError if it isn't matched.
            """

            # If we have an expexted result it will go here.
            expected = None

            if to_match is not None:
                parts = to_match.split('$')
                algorithm = parts[0]
                expected = parts[1]

            wrapped = getattr(hashlib, algorithm)()

            log.debug('Starting %s checksum to match %s', algorithm, expected)

            return (algorithm, wrapped, expected)

        def _update_checksum(self, checksum_in_progress, data):
            """
            Update a checksum in progress from _start_checksum with new data.
            """
            log.debug('Updating checksum with %d bytes', len(data))
            checksum_in_progress[1].update(data)

        def _finish_checksum(self, checksum_in_progress):
            """
            Complete a checksum in progress from _start_checksum and return the
            checksum result string.
            """

            result_hash = checksum_in_progress[1].hexdigest()

            log.debug('Completed checksum with hash %s vs. expected %s', result_hash, checksum_in_progress[2])

            if checksum_in_progress[2] is not None:
                # We expected a particular hash
                if result_hash != checksum_in_progress[2]:
                    raise ChecksumError('Checksum mismatch. Expected: %s Actual: %s' %
                                        (checksum_in_progress[2], result_hash))

            return '$'.join([checksum_in_progress[0], result_hash])

        def _get_file_checksum(self, localFilePath, to_match=None):
            with open(localFilePath, 'rb') as f:
                hasher = self._start_checksum(to_match=to_match)
                contents = f.read(1024 * 1024)
                while contents != b'':
                    self._update_checksum(hasher, contents)
                    contents = f.read(1024 * 1024)
                return self._finish_checksum(hasher)

        @contextmanager
        def uploadStream(self, multipart=True, allowInlining=True):
            """
            Context manager that gives out a binary-mode upload stream to upload data.
            """

            # Note that we have to handle already having a content or a version
            # if we are overwriting something.

            # But make sure we don't have both.
            assert not (bool(self.version) and self.content is not None)

            info = self
            store = self.outer

            class MultiPartPipe(WritablePipe):
                def readFrom(self, readable):
                    # Get the first block of data we want to put
                    buf = readable.read(store.partSize)
                    assert isinstance(buf, bytes)

                    if allowInlining and len(buf) <= info.maxInlinedSize():
                        log.debug('Inlining content of %d bytes', len(buf))
                        info.content = buf
                        # There will be no checksum
                        info.checksum = ''
                    else:
                        # We will compute a checksum
                        hasher = info._start_checksum()
                        info._update_checksum(hasher, buf)

                        # for attempt in retry_s3():
                        #     with attempt:
                        log.debug('Starting multipart upload')
                        client = store.s3_client
                        bucket_name = store.boto3FilesBucket.name
                        args = info._s3EncryptionArgs()

                        upload = client.create_multipart_upload(Bucket=bucket_name,
                                                                Key=compat_bytes(info.fileID), **args)
                        uploadId = upload['UploadId']
                        parts = []

                        try:
                            for part_num in itertools.count():
                                # for attempt in retry_s3():
                                #     with attempt:
                                log.debug('Uploading part %d of %d bytes', part_num + 1, len(buf))
                                part = client.upload_part(Bucket=bucket_name, Key=compat_bytes(info.fileID),
                                                          PartNumber=part_num + 1, UploadId=uploadId,
                                                          Body=BytesIO(buf), **args)

                                parts.append({"PartNumber": part_num + 1, "ETag": part["ETag"]})

                                # Get the next block of data we want to put
                                buf = readable.read(info.outer.partSize)
                                assert isinstance(buf, bytes)
                                if len(buf) == 0:
                                    # Don't allow any part other than the very first to be empty.
                                    break
                                info._update_checksum(hasher, buf)
                        except:
                            with panic(log=log):
                                for attempt in retry_s3():
                                    with attempt:
                                        client.abort_multipart_upload(Bucket=bucket_name, Key=compat_bytes(info.fileID),
                                                                      UploadId=uploadId)
                        else:

                            while not store._getBucketVersioningFromName(store.boto3FilesBucket.name):
                                log.warning('Versioning does not appear to be enabled yet. Deferring multipart upload '
                                            'completion...')
                                time.sleep(1)

                            # Save the checksum
                            info.checksum = info._finish_checksum(hasher)

                            # for attempt in retry_s3():
                            #     with attempt:
                            log.debug('Attempting to complete upload...')
                            completed = client.complete_multipart_upload(
                                Bucket=bucket_name, Key=compat_bytes(info.fileID), UploadId=uploadId,
                                MultipartUpload={"Parts": parts}, **args)
                            log.debug('Completed upload object of type %s: %s', str(type(completed)),
                                      repr(completed))
                            info.version = completed['VersionId']
                            log.debug('Completed upload with version %s', str(info.version))

                            if info.version is None:
                                # Somehow we don't know the version. Try and get it.
                                # for attempt in retry_s3(
                                #         predicate=lambda e: retryable_s3_errors(e) or isinstance(e, AssertionError)):
                                #     with attempt:
                                version = client.head_object(Bucket=bucket_name,
                                                             Key=compat_bytes(info.fileID), **args)['VersionId']
                                log.warning('Loaded key for upload with no version and got version %s', str(version))
                                info.version = version
                                assert info.version is not None

                    # Make sure we actually wrote something, even if an empty file
                    assert (bool(info.version) or info.content is not None)

            class SinglePartPipe(WritablePipe):
                def readFrom(self, readable):
                    buf = readable.read()
                    assert isinstance(buf, bytes)
                    dataLength = len(buf)
                    if allowInlining and dataLength <= info.maxInlinedSize():
                        log.debug('Inlining content of %d bytes', len(buf))
                        info.content = buf
                        # There will be no checksum
                        info.checksum = ''
                    else:
                        # We will compute a checksum
                        hasher = info._start_checksum()
                        info._update_checksum(hasher, buf)
                        info.checksum = info._finish_checksum(hasher)

                        bucket_name = store.boto3FilesBucket.name
                        args = info._s3EncryptionArgs()

                        obj = store.boto3FilesBucket.Object(compat_bytes(info.fileID))
                        buf = BytesIO(buf)

                        while not store._getBucketVersioningFromName(bucket_name):
                            log.warning('Versioning does not appear to be enabled yet. Deferring single part upload...')
                            time.sleep(1)

                        # for attempt in retry_s3():
                        #     with attempt:
                        log.debug('Uploading single part of %d bytes', dataLength)
                        obj.put(Body=buf, **args)
                        assert dataLength == obj.content_length
                        info.version = obj.version_id
                        log.debug('Upload received version %s', str(info.version))

                        if info.version is None:
                            # Somehow we don't know the version
                            # for attempt in retry_s3(
                            #         predicate=lambda e: retryable_s3_errors(e) or isinstance(e, AssertionError)):
                            #     with attempt:
                            obj.reload()
                            info.version = obj.version_id
                            log.warning('Reloaded key with no version and got version %s', str())
                            assert info.version is not None

                    # Make sure we actually wrote something, even if an empty file
                    assert (bool(info.version) or info.content is not None)

            pipe = MultiPartPipe() if multipart else SinglePartPipe()
            with pipe as writable:
                yield writable

            if not pipe.reader_done:
                log.debug('Version: {} Content: {}'.format(self.version, self.content))
                raise RuntimeError('Escaped context manager without written data being read!')

            # We check our work to make sure we have exactly one of embedded
            # content or a real object version.

            if self.content is None:
                if not bool(self.version):
                    log.debug('Version: {} Content: {}'.format(self.version, self.content))
                    raise RuntimeError('No content added and no version created')
            else:
                if bool(self.version):
                    log.debug('Version: {} Content: {}'.format(self.version, self.content))
                    raise RuntimeError('Content added and version created')

        def copyFrom(self, srcKey):
            """
            Copies contents of source key into this file.

            :param srcKey: The key that will be copied from
            """
            assert srcKey.size is not None
            if srcKey.size <= self.maxInlinedSize():
                self.content = srcKey.get_contents_as_string()
            else:
                self.version = copyKeyMultipart(srcBucketName=compat_plain(srcKey.bucket.name),
                                                srcKeyName=compat_plain(srcKey.name),
                                                srcKeyVersion=compat_plain(srcKey.version_id),
                                                dstBucketName=compat_plain(self.outer.filesBucket.name),
                                                dstKeyName=compat_plain(self._fileID),
                                                sseAlgorithm='AES256',
                                                sseKey=self._getSSEKey())

        def copyTo(self, dstKey):
            """
            Copies contents of this file to the given key.

            :param Key dstKey: The key to copy this file's content to
            """
            if self.content is not None:
                for attempt in retry_s3():
                    with attempt:
                        dstKey.set_contents_from_string(self.content)
            elif self.version:
                for attempt in retry_s3():
                    encrypted = True if self.outer.sseKeyPath else False
                    if encrypted:
                        srcKey = self.outer.filesBucket.get_key(compat_bytes(self.fileID),
                                                                headers=self._s3EncryptionHeaders())
                    else:
                        srcKey = self.outer.filesBucket.get_key(compat_bytes(self.fileID))
                    srcKey.version_id = self.version
                    with attempt:
                        copyKeyMultipart(srcBucketName=compat_plain(srcKey.bucket.name),
                                         srcKeyName=compat_plain(srcKey.name),
                                         srcKeyVersion=compat_plain(srcKey.version_id),
                                         dstBucketName=compat_plain(dstKey.bucket.name),
                                         dstKeyName=compat_plain(dstKey.name),
                                         copySourceSseAlgorithm='AES256',
                                         copySourceSseKey=self._getSSEKey())
            else:
                assert False

        def download(self, localFilePath, verifyChecksum=True):
            if self.content is not None:
                with AtomicFileCreate(localFilePath) as tmpPath:
                    with open(tmpPath, 'wb') as f:
                        f.write(self.content)
            elif self.version:
                args = self._s3EncryptionArgs()
                obj = self.outer.boto3FilesBucket.Object(compat_bytes(self.fileID))
                for attempt in retry_s3(predicate=lambda e: retryable_s3_errors(e) or isinstance(e, ChecksumError)):
                    with attempt:
                        with AtomicFileCreate(localFilePath) as tmpPath:
                            with open(tmpPath, 'wb') as content:
                                obj.download_fileobj(content, ExtraArgs={'VersionId': self.version, **args})

                        if verifyChecksum and self.checksum:
                            try:
                                # This automatically compares the result and matches the algorithm.
                                self._get_file_checksum(localFilePath, self.checksum)
                            except ChecksumError as e:
                                # Annotate checksum mismatches with file name
                                raise ChecksumError('Checksums do not match for file %s.' % localFilePath) from e
                                # The error will get caught and result in a retry of the download until we run out of retries.
                                # TODO: handle obviously truncated downloads by resuming instead.
            else:
                assert False

        @contextmanager
        def downloadStream(self, verifyChecksum=True):
            info = self

            class DownloadPipe(ReadablePipe):
                def writeTo(self, writable):
                    if info.content is not None:
                        writable.write(info.content)
                    elif info.version:
                        args = info._s3EncryptionArgs()
                        obj = info.outer.boto3FilesBucket.Object(compat_bytes(info.fileID))
                        # for attempt in retry_s3():
                        #     with attempt:
                        obj.download_fileobj(writable, ExtraArgs={'VersionId': info.version, **args})
                    else:
                        assert False

            class HashingPipe(ReadableTransformingPipe):
                """
                Class which checksums all the data read through it. If it
                reaches EOF and the checksum isn't correct, raises
                ChecksumError.

                Assumes info actually has a checksum.
                """

                def transform(self, readable, writable):
                    hasher = info._start_checksum(to_match=info.checksum)
                    contents = readable.read(1024 * 1024)
                    while contents != b'':
                        info._update_checksum(hasher, contents)
                        try:
                            writable.write(contents)
                        except BrokenPipeError:
                            # Read was stopped early by user code.
                            # Can't check the checksum.
                            return
                        contents = readable.read(1024 * 1024)
                    # We reached EOF in the input.
                    # Finish checksumming and verify.
                    info._finish_checksum(hasher)
                    # Now stop so EOF happens in the output.

            with DownloadPipe() as readable:
                if verifyChecksum and self.checksum:
                    # Interpose a pipe to check the hash
                    with HashingPipe(readable) as verified:
                        yield verified
                else:
                    # No true checksum available, so don't hash
                    yield readable

        def delete(self):
            # [!!] TODO: needs testing
            store = self.outer
            if self.previousVersion is not None:
                for attempt in retry_sdb():
                    with attempt:
                        store.filesDomain.delete_attributes(
                            compat_bytes(self.fileID),
                            expected_values=['version', self.previousVersion])
                if self.previousVersion:
                    # for attempt in retry_s3():
                    #     with attempt:
                    store.s3_client.delete_object(Bucket=store.boto3FilesBucket.name,
                                                  Key=compat_bytes(self.fileID),
                                                  VersionId=self.previousVersion)

        def getSize(self):
            """
            Return the size of the referenced item in bytes.
            """
            if self.content is not None:
                return len(self.content)
            elif self.version:
                for attempt in retry_s3():
                    with attempt:
                        key = self.outer.filesBucket.get_key(compat_bytes(self.fileID), validate=False)
                        return key.size
            else:
                return 0

        def _getSSEKey(self):
            sseKeyPath = self.outer.sseKeyPath
            if sseKeyPath is None:
                return None
            else:
                with open(sseKeyPath, 'rb') as f:
                    sseKey = f.read()
                    return sseKey

        def _s3EncryptionHeaders(self):
            if self.encrypted:
                sseKey = self._getSSEKey()
                assert sseKey is not None, 'Content is encrypted but no key was provided.'
                assert len(sseKey) == 32
                encodedSseKey = base64.b64encode(sseKey).decode('utf-8')
                encodedSseKeyMd5 = base64.b64encode(hashlib.md5(sseKey).digest()).decode('utf-8')
                return {'x-amz-server-side-encryption-customer-algorithm': 'AES256',
                        'x-amz-server-side-encryption-customer-key': encodedSseKey,
                        'x-amz-server-side-encryption-customer-key-md5': encodedSseKeyMd5}
            else:
                return {}

        def _s3EncryptionArgs(self):
            if self.encrypted:
                sseKey = self._getSSEKey()
                assert sseKey is not None, 'Content is encrypted but no key was provided.'
                assert len(sseKey) == 32
                encodedSseKey = base64.b64encode(sseKey).decode('utf-8')
                # encodedSseKeyMd5 = base64.b64encode(hashlib.md5(sseKey).digest()).decode('utf-8')
                # MD5 is supposedly calculated by boto3, though one can provide it via SSECustomerKeyMD5
                return {'SSECustomerAlgorithm': 'AES256', 'SSECustomerKey': encodedSseKey}
            else:
                return {}

        def __repr__(self):
            r = custom_repr
            d = (('fileID', r(self.fileID)),
                 ('ownerID', r(self.ownerID)),
                 ('encrypted', r(self.encrypted)),
                 ('version', r(self.version)),
                 ('previousVersion', r(self.previousVersion)),
                 ('content', r(self.content)),
                 ('checksum', r(self.checksum)),
                 ('_numContentChunks', r(self._numContentChunks)))
            return "{}({})".format(type(self).__name__,
                                   ', '.join('%s=%s' % (k, v) for k, v in d))

    versionings = dict(Enabled=True, Disabled=False, Suspended=None)

    def _getBucketVersioning(self, bucket):
        """
        For newly created buckets get_versioning_status returns an empty dict. In the past we've
        seen None in this case. We map both to a return value of False.

        Otherwise, the 'Versioning' entry in the dictionary returned by get_versioning_status can
        be 'Enabled', 'Suspended' or 'Disabled' which we map to True, None and False
        respectively. Note that we've never seen a versioning status of 'Disabled', only the
        empty dictionary. Calling configure_versioning with False on a bucket will cause
        get_versioning_status to then return 'Suspended' even on a new bucket that never had
        versioning enabled.
        """
        for attempt in retry_s3():
            with attempt:
                status = bucket.get_versioning_status()
                return self.versionings[status['Versioning']] if status else False

    def _getBucketVersioningFromName(self, bucket_name):
        """
        Get the Versioning property from a bucket name with boto3.

        Similar to _getBucketVersioning with the minor difference that BucketVersioning::status
        returns the status directly (not in a dictionary). For newly created buckets the
        status is None instead of an empty dict.

        :param bucket_name: str
        """
        # for attempt in retry_s3():
        #     with attempt:
        status = self.s3_resource.BucketVersioning(bucket_name).status
        return self.versionings[status] if status else False

    @staticmethod
    def __getBucketRegion(bucket_name):
        """
        :param bucket_name: str
        """
        # for attempt in retry_s3():
        #     with attempt:
        region = bucket_location_to_region(
            # use the 'global' resource here since it's not region specific
            s3_boto3_resource.meta.client.get_bucket_location(Bucket=bucket_name))['LocationConstraint']
        return region

    def destroy(self):
        # FIXME: Destruction of encrypted stores only works after initialize() or .resume()
        # See https://github.com/BD2KGenomics/toil/issues/1041
        try:
            self._bind(create=False, block=False, check_versioning_consistency=False)
        except BucketLocationConflictException:
            # If the unique jobstore bucket name existed, _bind would have raised a
            # BucketLocationConflictException before calling destroy.  Calling _bind here again
            # would reraise the same exception so we need to catch and ignore that exception.
            pass
        # TODO: Add other failure cases to be ignored here.
        self._registered = None
        if self.filesBucket is not None:
            self._delete_bucket(self.filesBucket)
            self.filesBucket = None
        for name in 'filesDomain', 'jobsDomain':
            domain = getattr(self, name)
            if domain is not None:
                self._delete_domain(domain)
                setattr(self, name, None)
        self._registered = False

    def _delete_domain(self, domain):
        for attempt in retry_sdb():
            with attempt:
                try:
                    domain.delete()
                except SDBResponseError as e:
                    if no_such_sdb_domain(e):
                        pass
                    else:
                        raise

    def _delete_bucket(self, bucket):
        """
        :param bucket: S3.Bucket
        """
        # for attempt in retry_s3():
        #     with attempt:
        try:
            for upload in bucket.list_multipart_uploads():
                upload.cancel_upload()  # TODO: upgrade this portion to boto3
            bucket = self.s3_resource.Bucket(compat_bytes(bucket.name))
            bucket.objects.all().delete()
            bucket.object_versions.delete()
            bucket.delete()
        except self.s3_resource.meta.client.exceptions.NoSuchBucket:
            pass
        except S3ResponseError as e:
            if e.error_code != 'NoSuchBucket':
                raise


aRepr = reprlib.Repr()
aRepr.maxstring = 38  # so UUIDs don't get truncated (36 for UUID plus 2 for quotes)
custom_repr = aRepr.repr


class BucketLocationConflictException(Exception):
    def __init__(self, bucketRegion):
        super(BucketLocationConflictException, self).__init__(
            'A bucket with the same name as the jobstore was found in another region (%s). '
            'Cannot proceed as the unique bucket name is already in use.' % bucketRegion)