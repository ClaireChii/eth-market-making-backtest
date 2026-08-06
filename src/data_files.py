"""Discover and validate the expected raw market-data files."""

from pathlib import Path

from src.config import DATASETS, DATA_ROOT, EXPECTED_DATES


def dataset_files(data_root: Path = DATA_ROOT) -> dict[str, list[Path]]:
    """Return the expected daily files, failing loudly on missing/extra dates."""
    discovered: dict[str, list[Path]] = {}
    expected_names = {f"{date}.parquet" for date in EXPECTED_DATES}

    for dataset in DATASETS:
        folder = data_root / dataset
        files = sorted(folder.glob("*.parquet"))
        names = {path.name for path in files}
        if names != expected_names:
            missing = sorted(expected_names - names)
            extra = sorted(names - expected_names)
            raise ValueError(
                f"{dataset}: unexpected daily files; missing={missing}, extra={extra}"
            )
        discovered[dataset] = files

    return discovered
