''' An input task for the `sentinelhub processing api <https://docs.sentinel-hub.com/api/latest/reference/>`
'''
import json
import logging
from copy import deepcopy
import datetime as dt
import numpy as np

from sentinelhub import WebFeatureService, MimeType, SentinelHubDownloadClient, DownloadRequest, SHConfig
import sentinelhub.sentinelhub_request as shr

from eolearn.core import EOPatch, EOTask, FeatureParser

LOGGER = logging.getLogger(__name__)


class SentinelHubProcessingInput(EOTask):
    ''' A processing API input task that loads 16bit integer data and converts it to a 32bit float feature.
    '''
    def __init__(self, data_source, size=None, resolution=None, bands_feature=None, bands=None, additional_data=None,
                 maxcc=1.0, time_difference=None, cache_folder=None, max_threads=5):
        """
        :param size: Number of pixels in x and y dimension.
        :type size_x: tuple(int, int)
        :param bands_feature: Target feature into which to save the downloaded images.
        :type bands_feature: tuple(sentinelhub.FeatureType, str)
        :param bands: An array of band names.
        :type bands: list[str]
        :param additional_data: A list of additional data to be downloaded, such as SCL, SNW, dataMask, etc.
        :type additional_data: list[tuple(sentinelhub.FeatureType, str)]
        :param maxcc: Maximum cloud coverage.
        :type maxcc: float
        :param time_difference: Minimum allowed time difference, used when filtering dates, None by default.
        :type time_difference: datetime.timedelta
        :param cache_folder: Path to cache_folder. If set to None (default) requests will not be cached.
        :type cache_folder: str
        :param max_threads: Maximum threads to be used when downloading data.
        :type max_threads: int
        """
        self.size = size
        self.resolution = resolution
        self.data_source = data_source
        self.maxcc = maxcc
        self.time_difference = dt.timedelta(seconds=1) if time_difference is None else time_difference
        self.cache_folder = cache_folder
        self.max_threads = max_threads

        self.bands_feature = bands_feature
        self.bands = bands or data_source.bands() if bands_feature else []
        self.additional_data = additional_data or []

        self.all_bands = self.bands + [f_name for _, f_name, _ in FeatureParser(self.additional_data, new_names=True)()]

    @staticmethod
    def request_from_date(request, date, maxcc, time_difference):
        ''' Make a deep copy of a request and sets it's (from, to) range according to the provided 'date' argument

        :param request: Path to cache_folder. If set to None (default) requests will not be cached.
        :type request: str
        '''

        date_from, date_to = date - time_difference, date + time_difference
        time_from, time_to = date_from.isoformat() + 'Z', date_to.isoformat() + 'Z'

        request = deepcopy(request)
        for data in request['input']['data']:
            time_range = data['dataFilter']['timeRange']
            time_range['from'] = time_from
            time_range['to'] = time_to

            # this should be moved to sentinelhub-py package, it was done here to avoid doing another release of sh-py
            data['dataFilter']['maxCloudCoverage'] = int(maxcc * 100)

        return request

    def generate_evalscript(self):
        ''' Generate the evalscript to be passed with the request, based on chosen bands
        '''
        evalscript = """
            function setup() {{
                return {{
                    input: [{{
                        bands: {bands},
                        units: "DN"
                    }}],
                    output: {{
                        id:"default",
                        bands: {num_bands},
                        sampleType: SampleType.UINT16
                    }}
                }}
            }}

            function updateOutputMetadata(scenes, inputMetadata, outputMetadata) {{
                outputMetadata.userData = {{ "norm_factor":  inputMetadata.normalizationFactor }}
            }}

            function evaluatePixel(sample) {{
                return {samples}
            }}
        """

        samples = ', '.join(['sample.{}'.format(band) for band in self.all_bands])
        samples = '[{}]'.format(samples)

        return evalscript.format(bands=json.dumps(self.all_bands), num_bands=len(self.all_bands), samples=samples)

    def get_dates(self, bbox, time_interval):
        ''' Make a WebFeatureService request to get dates and clean them according to self.time_difference
        '''
        wfs = WebFeatureService(
            bbox=bbox, time_interval=time_interval, data_source=self.data_source, maxcc=self.maxcc
        )

        dates = wfs.get_dates()

        if len(dates) == 0:
            raise ValueError("No available images for requested time range: {}".format(time_interval))

        dates = sorted(dates)
        dates = [dates[0]] + [d2 for d1, d2 in zip(dates[:-1], dates[1:]) if d2 - d1 > self.time_difference]
        return dates

    @staticmethod
    def size_from_resolution(bbox, resolution):
        ''' Calculate size_x and size_y based on provided bbox and resolution
        '''
        if not bbox.crs.is_utm():
            raise ValueError("Only UTM crs is supported.")

        bbox = list(bbox)
        size_x = (bbox[2] - bbox[0]) / resolution
        size_y = (bbox[3] - bbox[1]) / resolution

        if not (size_x.is_integer() and size_y.is_integer()):
            raise ValueError("BBox width and height in CRS units are not multiples of resolution.")

        return int(size_x), int(size_y)

    def execute(self, eopatch=None, bbox=None, time_interval=None):
        ''' Make a WFS request to get valid dates, download an image for each valid date and store it in an EOPatch

        :param eopatch:
        :type eopatch: EOPatch or None
        :param bbox: specifies the bounding box of the requested image. Coordinates must be in
                     the specified coordinate reference system. Required.
        :type bbox: BBox
        :param time_interval: time or time range for which to return the results, in ISO8601 format
                              (year-month-date, for example: ``2016-01-01``, or year-month-dateThours:minutes:seconds
                              format, i.e. ``2016-01-01T16:31:21``). When a single time is specified the request will
                              return data for that specific date, if it exists. If a time range is specified the result
                              is a list of all scenes between the specified dates conforming to the cloud coverage
                              criteria. Most recent acquisition being first in the list. For the latest acquisition use
                              ``latest``. Examples: ``latest``, ``'2016-01-01'``, or ``('2016-01-01', ' 2016-01-31')``
         :type time_interval: datetime.datetime, str, or tuple of datetime.datetime/str
        '''

        if self.size is not None:
            size_x, size_y = self.size
        elif self.resolution is not None:
            size_x, size_y = self.size_from_resolution(bbox, self.resolution)

        responses = [shr.response('default', 'image/tiff'), shr.response('userdata', 'application/json')]
        request = shr.body(
            request_bounds=shr.bounds(crs=bbox.crs.opengis_string, bbox=list(bbox)),
            request_data=[shr.data(data_type=self.data_source.api_identifier())],
            request_output=shr.output(size_x=size_x, size_y=size_y, responses=responses),
            evalscript=self.generate_evalscript()
        )

        request_args = dict(
            url=SHConfig().get_sh_processing_api_url(),
            headers={"accept": "application/tar", 'content-type': 'application/json'},
            data_folder=self.cache_folder,
            hash_save=bool(self.cache_folder),
            request_type='POST',
            data_type=MimeType.TAR
        )

        dates = self.get_dates(bbox, time_interval)
        requests = (self.request_from_date(request, date, self.maxcc, self.time_difference) for date in dates)
        requests = [DownloadRequest(post_values=payload, **request_args) for payload in requests]

        LOGGER.debug('Downloading %d requests of type %s', len(requests), str(self.data_source))
        LOGGER.debug('Downloading bands: [%s]', ', '.join(self.all_bands))
        client = SentinelHubDownloadClient()
        images = client.download(requests)
        LOGGER.debug('Downloads complete')

        images = ((img['default.tif'], img['userdata.json']) for img in images)
        images = [(img, meta.get('norm_factor', 0) if meta else 0) for img, meta in images]

        eopatch = EOPatch() if eopatch is None else eopatch

        eopatch.timestamp = dates
        eopatch.bbox = bbox

        shape = len(dates), size_y, size_x

        # extract additional_data from the received images each as a separate feature
        for f_type, f_name_src, f_name_dst in FeatureParser(self.additional_data, new_names=True)():
            idx = self.all_bands.index(f_name_src)
            feature_arrays = [np.atleast_3d(img)[..., idx] for img, norm_factor in images]
            eopatch[(f_type, f_name_dst)] = np.asarray(feature_arrays).reshape(*shape, 1).astype(np.bool)

        # extract bands from the received and save them as self.bands_feature
        if self.bands:
            img_bands = len(self.bands)
            img_arrays = [img[..., slice(img_bands)].astype(np.float32) * norm_factor for img, norm_factor in images]
            eopatch[self.bands_feature] = np.round(np.asarray(img_arrays).reshape(*shape, img_bands), 4)

        eopatch.meta_info['service_type'] = 'processing'
        eopatch.meta_info['size_x'] = size_x
        eopatch.meta_info['size_y'] = size_y
        eopatch.meta_info['resolution'] = self.resolution
        eopatch.meta_info['maxcc'] = self.maxcc
        eopatch.meta_info['time_interval'] = time_interval
        eopatch.meta_info['time_difference'] = self.time_difference

        return eopatch


class SentinelHubProcessingDEM(EOTask):
    ''' A processing API input task that loads 16bit integer data and converts it to a 32bit float feature.
    '''
    def __init__(self, data_source, size=None, bands_feature=None, bands=None, additional_data=None,
                 maxcc=1.0, time_difference=None, cache_folder=None, max_threads=5):
        """
        :param size: Number of pixels in x and y dimension.
        :type size_x: tuple(int, int)
        :param bands_feature: Target feature into which to save the downloaded images.
        :type bands_feature: tuple(sentinelhub.FeatureType, str)
        :param bands: An array of band names.
        :type bands: list[str]
        :param additional_data: A list of additional data to be downloaded, such as SCL, SNW, dataMask, etc.
        :type additional_data: list[tuple(sentinelhub.FeatureType, str)]
        :param maxcc: Maximum cloud coverage.
        :type maxcc: float
        :param time_difference: Minimum allowed time difference, used when filtering dates, None by default.
        :type time_difference: datetime.timedelta
        :param cache_folder: Path to cache_folder. If set to None (default) requests will not be cached.
        :type cache_folder: str
        :param max_threads: Maximum threads to be used when downloading data.
        :type max_threads: int
        """
        self.size = size
        self.data_source = data_source
        self.maxcc = maxcc
        self.time_difference = dt.timedelta(seconds=1) if time_difference is None else time_difference
        self.cache_folder = cache_folder
        self.max_threads = max_threads

        self.bands_feature = bands_feature
        self.bands = bands or data_source.bands() if bands_feature else []
        self.additional_data = additional_data or []

        self.all_bands = self.bands + [f_name for _, f_name, _ in FeatureParser(self.additional_data, new_names=True)()]

    def generate_evalscript(self):
        ''' Generate the evalscript to be passed with the request, based on chosen bands
        '''
        return """
            function setup() {
                return {
                    input: ["DEM"],
                    output:{
                        id: "default",
                        bands: 1,
                        sampleType: SampleType.UINT16
                    }
                }
            }

            function evaluatePixel(sample) {
                return [sample.DEM]
            }
        """

    def execute(self, eopatch=None, bbox=None, time_interval=None):
        ''' Make a WFS request to get valid dates, download an image for each valid date and store it in an EOPatch

        :param eopatch:
        :type eopatch: EOPatch or None
        :param bbox: specifies the bounding box of the requested image. Coordinates must be in
                     the specified coordinate reference system. Required.
        :type bbox: BBox
        :param time_interval: time or time range for which to return the results, in ISO8601 format
                              (year-month-date, for example: ``2016-01-01``, or year-month-dateThours:minutes:seconds
                              format, i.e. ``2016-01-01T16:31:21``). When a single time is specified the request will
                              return data for that specific date, if it exists. If a time range is specified the result
                              is a list of all scenes between the specified dates conforming to the cloud coverage
                              criteria. Most recent acquisition being first in the list. For the latest acquisition use
                              ``latest``. Examples: ``latest``, ``'2016-01-01'``, or ``('2016-01-01', ' 2016-01-31')``
         :type time_interval: datetime.datetime, str, or tuple of datetime.datetime/str
        '''

        size_x, size_y = self.size

        responses = [shr.response('default', 'image/tiff'), shr.response('userdata', 'application/json')]
        request = shr.body(
            request_bounds=shr.bounds(crs=bbox.crs.opengis_string, bbox=list(bbox)),
            request_data=[{"type": "DEM"}],
            request_output=shr.output(size_x=size_x, size_y=size_y, responses=responses),
            evalscript=self.generate_evalscript()
        )

        request_args = dict(
            url=SHConfig().get_sh_processing_api_url(),
            headers={"accept": "application/tar", 'content-type': 'application/json'},
            data_folder=self.cache_folder,
            hash_save=bool(self.cache_folder),
            request_type='POST',
            data_type=MimeType.TAR
        )

        request = DownloadRequest(post_values=request, **request_args)

        LOGGER.debug('Downloading DEM request')
        client = SentinelHubDownloadClient()
        image = client.download(request)
        LOGGER.debug('Download complete')

        tif = image['default.tif']
        # usr = image['userdata.json']

        eopatch = EOPatch() if eopatch is None else eopatch
        eopatch.bbox = bbox
        eopatch[self.bands_feature] = tif[..., np.newaxis].astype(np.int16)

        return eopatch
