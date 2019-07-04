#!/usr/bin/env python
"""
Package WAGL HDF5 Outputs

This will convert the HDF5 file (and sibling fmask/gqa files) into
GeoTIFFS (COGs) with datacube metadata using the DEA naming conventions
for files.
"""
import contextlib
import os
import re
import tempfile
from datetime import timedelta, datetime
from pathlib import Path
from typing import List, Sequence, Optional, Iterable, Any, Tuple, Dict, Mapping

import attr
import click
import h5py
import numpy
import rasterio
from boltons.iterutils import get_path, PathAccessError
from click import secho
from dateutil.tz import tzutc
from rasterio import DatasetReader

from eodatasets2 import images, serialise
from eodatasets2.assemble import DatasetAssembler
from eodatasets2.images import GridSpec
from eodatasets2.model import DatasetDoc
from eodatasets2.serialise import loads_yaml
from eodatasets2.ui import PathPath
from eodatasets2.utils import default_utc

_POSSIBLE_PRODUCTS = ("nbar", "nbart", "lambertian", "sbt")
_DEFAULT_PRODUCTS = ("nbar", "nbart")

_THUMBNAILS = {
    ("landsat-5", "nbar"): ("nbar:band03", "nbar:band02", "nbar:band01"),
    ("landsat-5", "nbart"): ("nbart:band03", "nbart:band02", "nbart:band01"),
    ("landsat-7", "nbar"): ("nbar:band03", "nbar:band02", "nbar:band01"),
    ("landsat-7", "nbart"): ("nbart:band03", "nbart:band02", "nbart:band01"),
    ("landsat-8", "nbar"): ("nbar:band04", "nbar:band03", "nbar:band02"),
    ("landsat-8", "nbart"): ("nbart:band04", "nbart:band03", "nbart:band02"),
}

os.environ["CPL_ZIP_ENCODING"] = "UTF-8"

# From the internal h5 name (after normalisation) to the package name.
MEASUREMENT_TRANSLATION = {"exiting": "exiting_angle", "incident": "incident_angle"}

FILENAME_TIF_BAND = re.compile(
    r"(?P<prefix>(?:.*_)?)(?P<band_name>B[0-9][A0-9]|B[0-9]*|B[0-9a-zA-z]*)"
    r"(?P<extension>\....)"
)
PRODUCT_SUITE_FROM_GRANULE = re.compile("(L1[GTPCS]{1,2})")


def find_h5_paths(h5_obj: h5py.Group, dataset_class: str = "") -> List[str]:
    """
    Find all objects in a h5 of the given class, returning their path.

    (class examples: IMAGE, TABLE. SCALAR)
    """
    items = []

    def _find(name, obj):
        if obj.attrs.get("CLASS") == dataset_class:
            items.append(name)

    h5_obj.visititems(_find)
    return items


def unpack_products(
    p: DatasetAssembler, product_list: Iterable[str], h5group: h5py.Group
) -> None:
    """
    Unpack and package the NBAR and NBART products.
    """
    # listing of all datasets of IMAGE CLASS type
    img_paths = find_h5_paths(h5group, "IMAGE")

    for product in product_list:
        with do(f"Starting {product}", heading=True):
            for pathname in [
                p for p in img_paths if "/{}/".format(product.upper()) in p
            ]:
                with do(f"Path {pathname!r}"):
                    dataset = h5group[pathname]
                    p.write_measurement_h5(f"{product}:{_band_name(dataset)}", dataset)

            if (p.platform, product) in _THUMBNAILS:
                red, green, blue = _THUMBNAILS[(p.platform, product)]
                with do(f"Thumbnailing {product}"):
                    p.write_thumbnail(red, green, blue, kind=product)


def _band_name(dataset: h5py.Dataset) -> str:
    """
    Devise a band name for the given dataset (using its attributes)
    """
    # What we have to work with:
    # >>> print(repr((dataset.attrs["band_id"], dataset.attrs["band_name"], dataset.attrs["alias"])))
    # ('1', 'BAND-1', 'Blue')

    band_name = dataset.attrs["band_id"]

    # A purely numeric id needs to be formatted 'band01' according to naming conventions.
    try:
        number = int(dataset.attrs["band_id"])
        band_name = f"band{number:02}"
    except ValueError:
        pass

    return band_name.lower().replace("-", "_")


def unpack_observation_attributes(
    p: DatasetAssembler,
    product_list: Iterable[str],
    h5group: h5py.Group,
    infer_datetime_range=False,
):
    """
    Unpack the angles + other supplementary datasets produced by wagl.
    Currently only the mode resolution group gets extracted.
    """
    resolution_groups = sorted(g for g in h5group.keys() if g.startswith("RES-GROUP-"))
    # Use the highest resolution as the ground sample distance.
    del p.properties["eo:gsd"]
    p.properties["eo:gsd"] = min(
        min(h5group[grp].attrs["resolution"]) for grp in resolution_groups
    )

    if len(resolution_groups) not in (1, 2):
        raise NotImplementedError(
            f"Unexpected set of res-groups. "
            f"Expected either two (with pan) or one (without pan), "
            f"got {resolution_groups!r}"
        )
    # Res groups are ordered in descending resolution, so res-group-0 is the highest resolution.
    # (ie. res-group-0 in landsat 7/8 is Panchromatic)
    # We only care about packaging OA data for the "common" bands: not panchromatic.
    # So we always pick the lowest resolution: the last (or only) group.
    res_grp = h5group[resolution_groups[-1]]

    def _write(section: str, dataset_names: Sequence[str]):
        """
        Write supplementary attributes as measurement.
        """
        for dataset_name in dataset_names:
            o = f"{section}/{dataset_name}"
            with do(f"Path {o!r} "):
                measurement_name = f"{dataset_name.lower()}".replace("-", "_")
                measurement_name = MEASUREMENT_TRANSLATION.get(
                    measurement_name, measurement_name
                )

                p.write_measurement_h5(
                    f"oa:{measurement_name}",
                    res_grp[o],
                    # We only use the product bands for valid data calc, not supplementary.
                    # According to Josh: Supplementary pixels outside of the product bounds are implicitly invalid.
                    expand_valid_data=False,
                    overviews=None,
                )

    _write(
        "SATELLITE-SOLAR",
        [
            "SATELLITE-VIEW",
            "SATELLITE-AZIMUTH",
            "SOLAR-ZENITH",
            "SOLAR-AZIMUTH",
            "RELATIVE-AZIMUTH",
            "TIME-DELTA",
        ],
    )
    _write("INCIDENT-ANGLES", ["INCIDENT", "AZIMUTHAL-INCIDENT"])
    _write("EXITING-ANGLES", ["EXITING", "AZIMUTHAL-EXITING"])
    _write("RELATIVE-SLOPE", ["RELATIVE-SLOPE"])
    _write("SHADOW-MASKS", ["COMBINED-TERRAIN-SHADOW"])

    timedelta_data = (
        res_grp["SATELLITE-SOLAR/TIME-DELTA"] if infer_datetime_range else None
    )
    with do("Contiguity", timedelta=bool(timedelta_data)):
        create_contiguity(
            p,
            product_list,
            resolution_yx=res_grp.attrs["resolution"],
            timedelta_data=timedelta_data,
        )


def create_contiguity(
    p: DatasetAssembler,
    product_list: Iterable[str],
    resolution_yx: Tuple[float, float],
    timedelta_product: str = "nbar",
    timedelta_data: numpy.ndarray = None,
):
    """
    Create the contiguity (all pixels valid) dataset.

    Write a contiguity mask file based on the intersection of valid data pixels across all
    bands from the input files.
    """

    with tempfile.TemporaryDirectory(prefix="contiguity-") as tmpdir:
        for product in product_list:
            product_image_files = [
                path
                for band_name, path in p.iter_measurement_paths()
                if band_name.startswith(f"{product.lower()}:")
            ]

            if not product_image_files:
                secho(f"No images found for requested product {product}", fg="red")
                continue

            (res_y, res_x) = resolution_yx

            # Build a temp vrt
            # S2 bands are different resolutions. Make them appear the same when taking contiguity.
            tmp_vrt_path = Path(tmpdir) / f"{product}.vrt"
            images.run_command(
                [
                    "gdalbuildvrt",
                    "-q",
                    "-resolution",
                    "user",
                    "-tr",
                    res_x,
                    res_y,
                    "-separate",
                    tmp_vrt_path,
                    *product_image_files,
                ],
                tmpdir,
            )

            with rasterio.open(tmp_vrt_path) as ds:
                ds: DatasetReader
                geobox = GridSpec.from_rio(ds)
                contiguity = numpy.ones((ds.height, ds.width), dtype="uint8")
                for band in ds.indexes:
                    # TODO Shouldn't this use ds.nodata instead of 0? Copied from the old packager.
                    contiguity &= ds.read(band) > 0

                p.write_measurement_numpy(
                    f"oa:{product.lower()}_contiguity",
                    contiguity,
                    geobox,
                    overviews=None,
                )

            # masking the timedelta_data with contiguity mask to get max and min timedelta within the NBAR product
            # footprint for Landsat sensor. For Sentinel sensor, it inherits from level 1 yaml file
            if timedelta_data is not None and product.lower() == timedelta_product:
                valid_timedelta_data = numpy.ma.masked_where(
                    contiguity == 0, timedelta_data
                )

                def _strip_timezone(d: datetime):
                    return d.astimezone(tz=tzutc()).replace(tzinfo=None)

                center_dt = numpy.datetime64(_strip_timezone(p.datetime))
                from_dt: numpy.datetime64 = center_dt + numpy.timedelta64(
                    int(float(numpy.ma.min(valid_timedelta_data)) * 1_000_000), "us"
                )
                to_dt: numpy.datetime64 = center_dt + numpy.timedelta64(
                    int(float(numpy.ma.max(valid_timedelta_data)) * 1_000_000), "us"
                )
                p.datetime_range = (from_dt.astype(datetime), to_dt.astype(datetime))


def _boolstyle(s):
    if s:
        return click.style("✓", fg="green")
    else:
        return click.style("✗", fg="yellow")


@contextlib.contextmanager
def do(name: str, heading=False, **fields):
    single_line = not heading

    def val(v: Any):
        if isinstance(v, bool):
            return _boolstyle(v)
        if isinstance(v, Path):
            return repr(str(v))
        return repr(v)

    if heading:
        name = f"\n{name}"
    fields = " ".join(f"{k}:{val(v)}" for k, v in fields.items())
    secho(f"{name} {fields} ", nl=not single_line, fg="blue" if heading else None)
    yield
    if single_line:
        secho("(done)")


def _extract_reference_code(p: DatasetAssembler, granule: str) -> Optional[str]:
    matches = None
    if p.platform.startswith("landsat"):
        matches = re.match(r"L\w\d(?P<reference_code>\d{6}).*", granule)
    elif p.platform.startswith("sentinel-2"):
        matches = re.match(r".*_T(?P<reference_code>\d{1,2}[A-Z]{3})_.*", granule)

    if matches:
        [reference_code] = matches.groups()
        # TODO name properly
        return reference_code
    return None


@attr.s(auto_attribs=True)
class Granule:
    name: str
    wagl_hdf5: Path
    wagl_metadata: Dict
    source_level1_metadata: DatasetDoc

    fmask_doc: Optional[Dict] = None
    fmask_image: Optional[Path] = None
    gqa_doc: Optional[Dict] = None

    @classmethod
    def for_path(cls, wagl_hdf5: Path, level1_metadata_path: Optional[Path] = None):

        if not wagl_hdf5.exists():
            raise ValueError(f"Input hdf5 doesn't exist {wagl_hdf5}")

        with h5py.File(wagl_hdf5, "r") as fid:
            granule_names = fid.keys()

            for granule_name in granule_names:
                wagl_doc_field = get_path(fid, (granule_name, "METADATA", "CURRENT"))
                if not wagl_doc_field:
                    raise ValueError(
                        f"Granule contains no wagl metadata: {granule_name} in {wagl_hdf5}"
                    )

                wagl_doc = loads_yaml(wagl_doc_field[()])

                level1 = (
                    serialise.from_path(level1_metadata_path)
                    if level1_metadata_path.exists()
                    else None
                )
                if not level1_metadata_path:
                    level1_tar_path = Path(
                        get_path(wagl_doc, ("source_datasets", "source_level1"))
                    )
                    level1_metadata_path = level1_tar_path.with_suffix(
                        ".odc-metadata.yaml"
                    )

                fmask_image = wagl_hdf5.with_name(f"{granule_name}.fmask.img")
                if not fmask_image.exists():
                    raise ValueError(f"Fmask not found {fmask_image}")

                fmask_doc_path = fmask_image.with_suffix(".yaml")
                if not fmask_doc_path.exists():
                    raise ValueError(f"Fmask doc not found {fmask_doc_path}")
                with fmask_doc_path.open("r") as fl:
                    fmask_doc = loads_yaml(fl)

                gqa_doc_path = wagl_hdf5.with_name(f"{granule_name}.gqa.yaml")
                if not gqa_doc_path.exists():
                    raise ValueError(f"GQA not found {gqa_doc_path}")
                with gqa_doc_path.open("r") as fl:
                    gqa_doc = loads_yaml(fl)

                yield cls(
                    name=granule_name,
                    wagl_hdf5=wagl_hdf5,
                    wagl_metadata=wagl_doc,
                    source_level1_metadata=level1,
                    fmask_doc=fmask_doc,
                    fmask_image=fmask_image,
                    gqa_doc=gqa_doc,
                )


def package(
    out_directory: Path,
    granule: Granule,
    included_products: Iterable[str] = _DEFAULT_PRODUCTS,
    include_oa: bool = True,
) -> Path:
    """
    Package an L2 product.

    :param out_directory:
        The dataset will be placed inside

    :param included_products:
        A list of imagery products to include in the package.
        Defaults to all products.

    :return:
        The output Path
    """
    included_products = tuple(s.lower() for s in included_products)

    with h5py.File(granule.wagl_hdf5, "r") as fid:
        granule_group = fid[granule.name]
        with DatasetAssembler(out_directory, naming_conventions="dea") as p:
            level1 = granule.source_level1_metadata
            p.add_source_dataset(level1, auto_inherit_properties=True)

            # It's a GA ARD product.
            p.producer = "ga.gov.au"
            p.product_family = "ard"

            # GA's collection 3 processes USGS Collection 1
            if p.properties["landsat:collection_number"] == 1:
                org_collection_number = 3
            else:
                raise NotImplementedError(f"Unsupported collection number.")
            # TODO: wagl's algorithm version should determine our dataset version number, right?
            p.dataset_version = f"{org_collection_number}.0.0"
            p.reference_code = _extract_reference_code(p, granule.name)

            p.properties["dea:processing_level"] = "level-2"

            _read_wagl_metadata(p, granule_group)
            _read_gqa_doc(p, granule.gqa_doc)
            _read_fmask_doc(p, granule.fmask_doc)

            unpack_products(p, included_products, granule_group)

            if include_oa:
                with do(f"Starting OA", heading=True):
                    unpack_observation_attributes(
                        p,
                        included_products,
                        granule_group,
                        infer_datetime_range=level1.platform.startswith("landsat"),
                    )
                if granule.fmask_image:
                    with do(f"Writing fmask from {granule.fmask_image} "):
                        p.write_measurement("oa:fmask", granule.fmask_image)

            with do("Finishing package"):
                return p.done()


def _flatten_dict(d: Mapping, prefix=None, separator=".") -> Iterable[Tuple[str, Any]]:
    """
    >>> dict(_flatten_dict({'a' : 1, 'b' : {'inner' : 2},'c' : 3}))
    {'a': 1, 'b.inner': 2, 'c': 3}
    >>> dict(_flatten_dict({'a' : 1, 'b' : {'inner' : {'core' : 2}}}, prefix='outside', separator=':'))
    {'outside:a': 1, 'outside:b:inner:core': 2}
    """
    for k, v in d.items():
        name = f"{prefix}{separator}{k}" if prefix else k
        if isinstance(v, Mapping):
            yield from _flatten_dict(v, prefix=name, separator=separator)
        else:
            yield name, v


def _read_gqa_doc(p: DatasetAssembler, doc: Dict):
    _take_software_versions(p, doc)
    p.extend_user_metadata("gqa", doc)

    # TODO: more of the GQA fields?
    for k, v in _flatten_dict(doc["residual"], separator="_"):
        p.properties[f"gqa:{k}"] = v


def _read_fmask_doc(p: DatasetAssembler, doc: Dict):
    for name, value in doc["percent_class_distribution"].items():
        p.properties[f"fmask:{name}"] = value

    _take_software_versions(p, doc)
    p.extend_user_metadata("fmask", doc)


def _take_software_versions(p: DatasetAssembler, doc: Dict):
    versions = doc.pop("software_versions", {})

    for name, o in versions.items():
        p.note_software_version(name, o.get("repo_url"), o.get("version"))


def find_a_granule_name(wagl_hdf5: Path) -> str:
    """
    Try to extract granule name from wagl filename,

    >>> find_a_granule_name(Path('LT50910841993188ASA00.wagl.h5'))
    'LT50910841993188ASA00'
    >>> find_a_granule_name(Path('my-test-granule.h5'))
    Traceback (most recent call last):
    ...
    ValueError: No granule specified, and cannot find it on input filename 'my-test-granule'.
    """
    granule_name = wagl_hdf5.stem.split(".")[0]
    if not granule_name.startswith("L"):
        raise ValueError(
            f"No granule specified, and cannot find it on input filename {wagl_hdf5.stem!r}."
        )
    return granule_name


def _read_wagl_metadata(p: DatasetAssembler, granule_group: h5py.Group):
    try:
        wagl_path, *ancil_paths = [
            pth for pth in (find_h5_paths(granule_group, "SCALAR")) if "METADATA" in pth
        ]
    except ValueError:
        raise ValueError("No nbar metadata found in granule")

    wagl_doc = loads_yaml(granule_group[wagl_path][()])

    try:
        p.processed = get_path(wagl_doc, ("system_information", "time_processed"))
    except PathAccessError:
        raise ValueError(f"WAGL dataset contains no time processed. Path {wagl_path}")

    for i, path in enumerate(ancil_paths, start=2):
        wagl_doc.setdefault(f"wagl_{i}", {}).update(
            loads_yaml(granule_group[path][()])["ancillary"]
        )

    p.properties["dea:dataset_maturity"] = _determine_maturity(
        p.datetime, p.processed, wagl_doc
    )

    _take_software_versions(p, wagl_doc)
    p.extend_user_metadata("wagl", wagl_doc)


def _determine_maturity(acq_date: datetime, processed: datetime, wagl_doc: Dict):
    """
    Determine maturity field of a dataset.

    Based on the fallback logic in nbar pages of CMI, eg: https://cmi.ga.gov.au/ga_ls5t_nbart_3
    """
    ancillary_tiers = {
        key.lower(): o["tier"]
        for key, o in wagl_doc["ancillary"].items()
        if "tier" in o
    }

    if "water_vapour" not in ancillary_tiers:
        # Perhaps this should be a warning, but I'm being strict until told otherwise.
        # (a warning is easy to ignore)
        raise ValueError(
            f"No water vapour ancillary tier. Got {list(ancillary_tiers.keys())!r}"
        )

    water_vapour_is_definitive = ancillary_tiers["water_vapour"].lower() == "definitive"

    if (processed - acq_date) < timedelta(hours=48):
        return "nrt"

    if not water_vapour_is_definitive:
        return "interim"

    if acq_date < default_utc(datetime(2001, 1, 1)):
        return "final"

    brdf_tiers = {k: v for k, v in ancillary_tiers.items() if k.startswith("brdf_")}
    if not brdf_tiers:
        # Perhaps this should be a warning, but I'm being strict until told otherwise.
        # (a warning is easy to ignore)
        raise ValueError(
            f"No brdf tiers available. Got {list(ancillary_tiers.keys())!r}"
        )

    brdf_is_definitive = all([v.lower() == "definitive" for k, v in brdf_tiers.items()])

    if brdf_is_definitive:
        return "final"
    else:
        return "interim"


@click.command(help=__doc__)
@click.option(
    "--level1",
    help="An explicit path to the input level1 metadata doc "
    "(otherwise it will be loaded from the level1 path in the HDF5)",
    required=False,
    type=PathPath(exists=True, readable=True, dir_okay=False, file_okay=True),
)
@click.option(
    "--output",
    help="Put the output package into this directory",
    required=True,
    type=PathPath(exists=True, writable=True, dir_okay=True, file_okay=False),
)
@click.option(
    "-p",
    "--product",
    "products",
    help="Package only the given products (can specify multiple times)",
    type=click.Choice(_POSSIBLE_PRODUCTS, case_sensitive=False),
    multiple=True,
)
@click.option(
    "--with-oa/--no-oa",
    "with_oa",
    help="Include observation attributes (default: true)",
    is_flag=True,
    default=True,
)
@click.argument("h5_file", type=PathPath(exists=True, readable=True, writable=False))
def run(
    level1: Path, output: Path, h5_file: Path, products: Sequence[str], with_oa: bool
):
    if products:
        products = set(p.lower() for p in products)
    else:
        products = _DEFAULT_PRODUCTS
    with rasterio.Env():
        for granule in Granule.for_path(h5_file, level1_metadata_path=level1):
            with do(
                f"Packaging {granule.name}. (products: {', '.join(products)})",
                heading=True,
                fmask=bool(granule.fmask_image),
                fmask_doc=bool(granule.fmask_doc),
                gqa=bool(granule.gqa_doc),
                oa=with_oa,
            ):
                dataset_id, dataset_path = package(
                    out_directory=output,
                    granule=granule,
                    included_products=products,
                    include_oa=with_oa,
                )
                secho(f"Created folder {click.style(str(dataset_path), fg='green')}")


if __name__ == "__main__":
    run()
