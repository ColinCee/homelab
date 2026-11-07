"""The SSD wear reader (scripts/ssd_wear.py) turns an NVMe health log into ssd.prom."""

from pathlib import Path

import pytest

from scripts import ssd_wear


def health_log() -> bytes:
    log = bytearray(512)
    log[3] = 97  # available spare, percent
    log[5] = 12  # percentage used
    log[32:48] = (2_000_000).to_bytes(16, "little")  # data units read
    log[48:64] = (300_000_000).to_bytes(16, "little")  # data units written, about 150 TB
    return bytes(log)


def test_a_health_log_becomes_bytes_and_wear() -> None:
    text = ssd_wear.render(health_log())

    samples = [line for line in text.splitlines() if not line.startswith("#")]
    assert samples == [
        'homelab_ssd_written_bytes_total{device="nvme0"} 153600000000000',
        'homelab_ssd_read_bytes_total{device="nvme0"} 1024000000000',
        'homelab_ssd_percentage_used{device="nvme0"} 12',
        'homelab_ssd_available_spare_ratio{device="nvme0"} 0.97',
    ]
    assert "# TYPE homelab_ssd_written_bytes_total counter" in text


def test_a_short_log_leaves_the_old_file(tmp_path: Path) -> None:
    out = tmp_path / "ssd.prom"
    ssd_wear.write(ssd_wear.render(health_log()), out)
    before = out.read_text()

    with pytest.raises(ValueError):
        ssd_wear.write(ssd_wear.render(health_log()[:100]), out)

    assert out.read_text() == before
    assert [p.name for p in tmp_path.iterdir()] == ["ssd.prom"]
