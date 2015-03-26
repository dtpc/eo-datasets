import datetime
from os.path import join, dirname, relpath

from gaip.mtl import load_mtl
import eodatasets.type as ptype


def _get(dictionary, *keys):
    """

    :type dictionary: dict
    :type keys: list of str

    >>> _get({'b': 4, 'a': 2}, 'a')
    2
    >>> _get({'a': {'b': 4}}, 'a', 'b')
    4
    >>> _get({'c': {'b': 4}}, 'a', 'b')
    """
    s = dictionary
    for k in keys:
        if k not in s:
            return None

        s = s[k]
    return s


def _read_mtl_band_filenames(mtl_):
    """
    Read the list of bands from an mtl dictionary.
    :type mtl_: dict of (str, obj)
    :rtype: dict of (str, str)
    
    >>> _read_mtl_band_filenames({'PRODUCT_METADATA': {
    ...    'file_name_band_9': "LC81010782014285LGN00_B9.TIF",
    ...    'file_name_band_11': "LC81010782014285LGN00_B11.TIF",
    ...    'file_name_band_quality': "LC81010782014285LGN00_BQA.TIF"
    ...    }})
    {'9': 'LC81010782014285LGN00_B9.TIF', '11': 'LC81010782014285LGN00_B11.TIF', 'quality': 'LC81010782014285LGN00_BQA.TIF'}
    >>> _read_mtl_band_filenames({'PRODUCT_METADATA': {
    ...    'file_name_band_9': "LC81010782014285LGN00_B9.TIF",
    ...    'corner_ul_lat_product': -24.98805,
    ...    }})
    {'9': 'LC81010782014285LGN00_B9.TIF'}
    """
    product_md = mtl_['PRODUCT_METADATA']
    return dict([(k.split('_')[-1], v) for (k, v) in product_md.items() if k.startswith('file_name_band_')])


def _read_bands(mtl_, path_offset):
    """

    :param mtl_:
    :param relative_from_dir:
    >>> _read_bands({'PRODUCT_METADATA': {
    ...     'file_name_band_9': "LC81010782014285LGN00_B9.TIF"}
    ... }, path_offset='product/')
    {'9': BandMetadata(path='product/LC81010782014285LGN00_B9.TIF')}
    """
    bs = _read_mtl_band_filenames(mtl_)
    # TODO: shape, size, md5
    return dict([(number, ptype.BandMetadata(path=join(path_offset, filename)))
                 for (number, filename) in bs.items()])


def read_mtl(mtl_path, output_dir, md=None):
    """

    :param mtl_path: Path to mtl file
    :param metadata_directory: directory where this metadata will reside (for calculating relative band paths)
    :type md: eodatasets.type.DatasetMetadata
    :return:
    """

    if not md:
        md = ptype.DatasetMetadata()

    mtl_ = load_mtl(mtl_path)
    mtl_dir = relpath(dirname(mtl_path), start=output_dir)

    # md.id_=None,
    # md.ga_label=None,
    md.usgs_dataset_id = _get(mtl_, 'metadata_file_info', 'landsat_scene_id') or md.usgs_dataset_id
    md.creation_dt = _get(mtl_, 'metadata_file_info', 'file_date')
    # md.product_type=None,

    # md.size_bytes=None,
    md.platform.code = _get(mtl_, 'PRODUCT_METADATA', 'spacecraft_id')

    md.instrument.name = _get(mtl_, 'PRODUCT_METADATA', 'sensor_id')
    # type
    # operation mode

    # md.format_=None,

    md.acquisition.groundstation = ptype.GroundstationMetadata(code=_get(mtl_, "METADATA_FILE_INFO", "STATIONID"))
    # md.acquisition.groundstation.antenna_coord
    # aos, los, groundstation, heading, platform_orbit

    # Extent
    product_md = _get(mtl_, 'PRODUCT_METADATA')

    date = _get(product_md, 'date_acquired')
    center_time = _get(product_md, 'scene_center_time')
    md.extent.center_dt = datetime.datetime.combine(date, center_time)
    # md.extent.reference_system = ?

    md.extent.coord = ptype.Polygon(
        ul=ptype.Coord(lat=_get(product_md, 'corner_ul_lat_product'), lon=_get(product_md, 'corner_ul_lon_product')),
        ur=ptype.Coord(lat=_get(product_md, 'corner_ur_lat_product'), lon=_get(product_md, 'corner_ur_lon_product')),
        ll=ptype.Coord(lat=_get(product_md, 'corner_ll_lat_product'), lon=_get(product_md, 'corner_ll_lon_product')),
        lr=ptype.Coord(lat=_get(product_md, 'corner_lr_lat_product'), lon=_get(product_md, 'corner_lr_lon_product')),
    )
    # from_dt=None,
    # to_dt=None

    # We don't have a single set of dimensions. Depends on the band?
    # md.grid_spatial.dimensions = []   
    md.grid_spatial.projection.geo_ref_points = ptype.Polygon(
        ul=ptype.Point(x=_get(product_md, 'corner_ul_projection_x_product'),
                       y=_get(product_md, 'corner_ul_projection_y_product')),
        ur=ptype.Point(x=_get(product_md, 'corner_ur_projection_x_product'),
                       y=_get(product_md, 'corner_ur_projection_y_product')),
        ll=ptype.Point(x=_get(product_md, 'corner_ll_projection_x_product'),
                       y=_get(product_md, 'corner_ll_projection_y_product')),
        lr=ptype.Point(x=_get(product_md, 'corner_lr_projection_x_product'),
                       y=_get(product_md, 'corner_lr_projection_y_product'))
    )
    # centre_point=None,
    projection_md = _get(mtl_, 'PROJECTION_PARAMETERS')
    md.grid_spatial.projection.datum = _get(projection_md, 'datum')
    md.grid_spatial.projection.ellipsoid = _get(projection_md, 'ellipsoid')

    # Where does this come from? 'ul' etc.
    # point_in_pixel=None,
    md.grid_spatial.projection.map_projection = _get(projection_md, 'map_projection')
    # resampling_option=None,
    md.grid_spatial.projection.map_projection = _get(projection_md, 'map_projection')
    md.grid_spatial.projection.datum = _get(projection_md, 'datum')
    md.grid_spatial.projection.ellipsoid = _get(projection_md, 'ellipsoid')
    md.grid_spatial.projection.zone = _get(projection_md, 'utm_zone')

    # md.grid_spatial.projection. = _get(projection_md, 'orientation') # "NORTH_UP"
    # md.grid_spatial.projection. = _get(projection_md, 'resampling_option') # "CUBIC_CONVOLUTION"

    # No browse image
    # md.browse=None,

    image_md = _get(mtl_, 'IMAGE_ATTRIBUTES')

    md.image.satellite_ref_point_start = ptype.Point(
        _get(product_md, 'wrs_path'),
        _get(product_md, 'wrs_row')
    )

    md.image.cloud_cover_percentage = _get(image_md, 'cloud_cover')
    md.image.sun_elevation = _get(image_md, 'sun_elevation')
    md.image.sun_azimuth = _get(image_md, 'sun_azimuth')

    md.image.ground_control_points_model = _get(image_md, 'ground_control_points_model')
    # md.image. = _get(image_md, 'earth_sun_distance')
    md.image.geometric_rmse_model = _get(image_md, 'geometric_rmse_model')
    md.image.geometric_rmse_model_y = _get(image_md, 'geometric_rmse_model_y')
    md.image.geometric_rmse_model_x = _get(image_md, 'geometric_rmse_model_x')

    md.image.bands.update(_read_bands(mtl_, mtl_dir))

    # Example "LPGS_2.3.0"
    soft_v = _get(mtl_, 'METADATA_FILE_INFO', 'processing_software_version')
    md.lineage.algorithm.name, md.lineage.algorithm.version = soft_v.split('_')

    md.lineage.algorithm.parameters = {}  # ? TODO

    md.lineage.ancillary.update({
        'cpf': ptype.AncillaryMetadata(name=_get(product_md, 'cpf_name')),
        'bpf_oli': ptype.AncillaryMetadata(name=_get(product_md, 'bpf_name_oli')),
        'bpf_tirs': ptype.AncillaryMetadata(name=_get(product_md, 'bpf_name_tirs')),
        'rlut': ptype.AncillaryMetadata(name=_get(product_md, 'rlut_file_name'))
    })

    return md


def new_dataset_md(uuid=None):
    """
    Create blank metadata for a newly created dataset on this machine.
    :param uuid: The existing dataset_id, if any.
    :rtype: ptype.DatasetMetadata
    """
    md = ptype.DatasetMetadata(
        id_=uuid,
        platform=ptype.PlatformMetadata(),
        instrument=ptype.InstrumentMetadata(),
        acquisition=ptype.AcquisitionMetadata(),
        extent=ptype.ExtentMetadata(),
        grid_spatial=ptype.GridSpatialMetadata(projection=ptype.ProjectionMetadata()),
        image=ptype.ImageMetadata(bands={}),
        lineage=ptype.LineageMetadata(
            algorithm=ptype.AlgorithmMetadata(),
            machine=ptype.MachineMetadata(),
            ancillary={},
            source_datasets={}
        )
    )
    return md


if __name__ == '__main__':
    # import doctest
    # doctest.testmod(type)
    package_dir = '/Users/jeremyhooke/ops/package-eg/LS8_OLITIRS_OTH_P51_GALPGS01-032_101_078_20141012'
    mtl_path = package_dir + '/scene01/LC81010782014285LGN00_MTL.txt'

    d = new_dataset_md()
    m = read_mtl(mtl_path, package_dir, md=d)

    print ptype.yaml.dump(d, default_flow_style=False, indent=4)


