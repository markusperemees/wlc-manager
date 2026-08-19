from pathlib import Path

import pytest

from wlc_manager.artifacts import (
    ArtifactError,
    PosterGenerator,
    cleanup_old_artifacts,
    wifi_qr_payload,
)
from wlc_manager.config import ArtifactConfig
from wlc_manager.database import PasswordRecord, PasswordState
from wlc_manager.scheduling import YearMonth

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = PROJECT_ROOT / "templates" / "wifi-poster.svg"


def _cairo_runtime_available() -> bool:
    try:
        import cairosvg  # noqa: F401
    except Exception:
        return False
    return True


def _config(tmp_path: Path, *, template: Path = TEMPLATE) -> ArtifactConfig:
    return ArtifactConfig(
        svg_template_path=template,
        output_directory=tmp_path / "artifacts",
        security_label="WPA2",
        qr_auth_type="WPA",
        qr_size_mm=60,
        png_dpi=150,
    )


def _record(period: str = "2026-09") -> PasswordRecord:
    return PasswordRecord(
        id=1,
        validity_month=period,
        password="markus123apple",
        dictionary_word="apple",
        state=PasswordState.GENERATED,
        created_at="2026-08-27T08:00:00Z",
        run_id="run-1",
    )


def test_wifi_qr_payload_escapes_reserved_characters() -> None:
    payload = wifi_qr_payload(
        ssid=r"Guest;Floor,1:East\WiFi",
        password=r"pass;word,one:two\three",
    )

    assert payload == (
        r"WIFI:T:WPA;S:Guest\;Floor\,1\:East\\WiFi;"
        r"P:pass\;word\,one\:two\\three;;"
    )


def test_template_is_filled_xml_safely_and_contains_vector_qr(tmp_path: Path) -> None:
    generator = PosterGenerator(_config(tmp_path))

    rendered = generator.render_svg(_record(), ssid="Guest & <Public>").decode("utf-8")

    assert "{{" not in rendered
    assert "Guest &amp; &lt;Public&gt;" in rendered
    assert "markus123apple" in rendered
    assert "WPA2" in rendered
    assert 'shape-rendering="crispEdges"' in rendered
    assert 'width="60" height="60"' in rendered
    assert "<path" in rendered


@pytest.mark.skipif(not _cairo_runtime_available(), reason="native Cairo runtime is unavailable")
def test_real_png_and_pdf_generation_is_idempotent(tmp_path: Path) -> None:
    generator = PosterGenerator(_config(tmp_path))

    first = generator.generate(_record(), ssid="Public-WiFi", current_period=YearMonth(2026, 8))
    second = generator.generate(_record(), ssid="Public-WiFi", current_period=YearMonth(2026, 8))

    assert first.created
    assert not second.created
    assert first.png_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert first.pdf_path.read_bytes().startswith(b"%PDF-")
    assert first.png_path.stat().st_size > 1000
    assert first.pdf_path.stat().st_size > 1000


def test_old_managed_artifacts_are_removed_but_unrelated_files_remain(tmp_path: Path) -> None:
    output = tmp_path / "artifacts"
    output.mkdir()
    old_png = output / "wifi-2026-07.png"
    old_pdf = output / "wifi-2026-07.pdf"
    current = output / "wifi-2026-08.png"
    next_month = output / "wifi-2026-09.pdf"
    unrelated = output / "notes.txt"
    for path in (old_png, old_pdf, current, next_month, unrelated):
        path.write_text("test", encoding="utf-8")

    removed = cleanup_old_artifacts(output, current_period=YearMonth(2026, 8))

    assert set(removed) == {old_png, old_pdf}
    assert not old_png.exists()
    assert not old_pdf.exists()
    assert current.exists()
    assert next_month.exists()
    assert unrelated.exists()


def test_conversion_failure_does_not_publish_artifacts(tmp_path: Path) -> None:
    def png_converter(**kwargs) -> None:
        Path(kwargs["write_to"]).write_bytes(b"\x89PNG\r\n\x1a\nvalid")

    def pdf_converter(**kwargs) -> None:
        raise RuntimeError("conversion failed")

    generator = PosterGenerator(
        _config(tmp_path),
        png_converter=png_converter,
        pdf_converter=pdf_converter,
    )

    with pytest.raises(ArtifactError, match="poster conversion failed"):
        generator.generate(_record(), ssid="Public-WiFi", current_period=YearMonth(2026, 8))

    assert not (tmp_path / "artifacts" / "wifi-2026-09.png").exists()
    assert not (tmp_path / "artifacts" / "wifi-2026-09.pdf").exists()
    assert list((tmp_path / "artifacts").iterdir()) == []


def test_template_must_contain_all_known_placeholders(tmp_path: Path) -> None:
    template = tmp_path / "invalid.svg"
    template.write_text(
        "<svg xmlns='http://www.w3.org/2000/svg'>{{PASSWORD}}</svg>", encoding="utf-8"
    )
    generator = PosterGenerator(_config(tmp_path, template=template))

    with pytest.raises(ArtifactError, match="missing placeholders"):
        generator.render_svg(_record(), ssid="Public-WiFi")
