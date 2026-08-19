from __future__ import annotations

import os
import re
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import qrcode
from defusedxml.ElementTree import fromstring
from qrcode.constants import ERROR_CORRECT_Q

from wlc_manager.config import ArtifactConfig
from wlc_manager.database import PasswordRecord, PasswordRepository, PasswordState
from wlc_manager.scheduling import YearMonth

_SVG_NAMESPACE = "http://www.w3.org/2000/svg"
_PLACEHOLDER_PATTERN = re.compile(r"\{\{[A-Z][A-Z0-9_]*\}\}")
_REQUIRED_PLACEHOLDERS = {"{{QR_CODE}}", "{{SSID}}", "{{PASSWORD}}", "{{SECURITY}}"}
_MANAGED_ARTIFACT_PATTERN = re.compile(
    r"^wifi-(?P<period>[0-9]{4}-(?:0[1-9]|1[0-2]))\.(?P<format>png|pdf)$"
)


class ArtifactError(RuntimeError):
    """Raised when poster artifacts cannot be generated safely."""


@dataclass(frozen=True, slots=True)
class ArtifactFiles:
    period: YearMonth
    png_path: Path
    pdf_path: Path
    created: bool


@dataclass(frozen=True, slots=True)
class ArtifactReconciliationResult:
    checked_periods: tuple[YearMonth, ...]
    resolved_files: tuple[ArtifactFiles, ...]


class MonthlyArtifactReconciler:
    """Ensure poster files exist for the current and next password records."""

    def __init__(
        self,
        repository: PasswordRepository,
        generator: PosterGenerator,
        *,
        ssid: str,
    ) -> None:
        self.repository = repository
        self.generator = generator
        self.ssid = ssid

    def reconcile(self, *, current_period: YearMonth) -> ArtifactReconciliationResult:
        periods = (current_period, current_period.next())
        resolved: list[ArtifactFiles] = []
        for period in periods:
            record = self.repository.get_by_month(str(period))
            if record is None or record.state is PasswordState.EXPIRED:
                continue
            files = self.generator.generate(
                record,
                ssid=self.ssid,
                current_period=current_period,
            )
            if record.state in {PasswordState.GENERATED, PasswordState.MATERIALS_CREATED}:
                self.repository.mark_materials_created(
                    str(period),
                    png_path=files.png_path,
                    pdf_path=files.pdf_path,
                )
            resolved.append(files)

        cleanup_old_artifacts(
            self.generator.config.output_directory,
            current_period=current_period,
        )
        return ArtifactReconciliationResult(
            checked_periods=periods,
            resolved_files=tuple(resolved),
        )


class PosterGenerator:
    def __init__(
        self,
        config: ArtifactConfig,
        *,
        png_converter: Callable[..., Any] | None = None,
        pdf_converter: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self.png_converter = png_converter or _cairosvg_png
        self.pdf_converter = pdf_converter or _cairosvg_pdf

    def generate(
        self,
        record: PasswordRecord,
        *,
        ssid: str,
        current_period: YearMonth,
    ) -> ArtifactFiles:
        period = YearMonth.parse(record.validity_month)
        output_directory = self.config.output_directory
        output_directory.mkdir(parents=True, exist_ok=True)
        png_path = output_directory / f"wifi-{period}.png"
        pdf_path = output_directory / f"wifi-{period}.pdf"

        if _is_nonempty_file(png_path) and _is_nonempty_file(pdf_path):
            cleanup_old_artifacts(output_directory, current_period=current_period)
            return ArtifactFiles(
                period=period,
                png_path=png_path,
                pdf_path=pdf_path,
                created=False,
            )

        svg_bytes = self.render_svg(record, ssid=ssid)
        temporary_png = _temporary_path(output_directory, png_path.name)
        temporary_pdf = _temporary_path(output_directory, pdf_path.name)
        try:
            self.png_converter(
                bytestring=svg_bytes,
                write_to=str(temporary_png),
                dpi=self.config.png_dpi,
            )
            self.pdf_converter(
                bytestring=svg_bytes,
                write_to=str(temporary_pdf),
            )
            _validate_generated_file(temporary_png, expected="png")
            _validate_generated_file(temporary_pdf, expected="pdf")
            os.replace(temporary_png, png_path)
            os.replace(temporary_pdf, pdf_path)
        except Exception as exc:
            temporary_png.unlink(missing_ok=True)
            temporary_pdf.unlink(missing_ok=True)
            if isinstance(exc, ArtifactError):
                raise
            raise ArtifactError(f"poster conversion failed ({type(exc).__name__})") from None

        cleanup_old_artifacts(output_directory, current_period=current_period)
        return ArtifactFiles(
            period=period,
            png_path=png_path,
            pdf_path=pdf_path,
            created=True,
        )

    def render_svg(self, record: PasswordRecord, *, ssid: str) -> bytes:
        try:
            template = self.config.svg_template_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ArtifactError(f"cannot read UTF-8 SVG template ({type(exc).__name__})") from None

        placeholders = set(_PLACEHOLDER_PATTERN.findall(template))
        missing = _REQUIRED_PLACEHOLDERS - placeholders
        unknown = placeholders - _REQUIRED_PLACEHOLDERS
        if missing:
            raise ArtifactError(
                f"SVG template is missing placeholders: {', '.join(sorted(missing))}"
            )
        if unknown:
            raise ArtifactError(
                f"SVG template contains unknown placeholders: {', '.join(sorted(unknown))}"
            )

        try:
            root = fromstring(template)
        except Exception as exc:
            raise ArtifactError(
                f"SVG template is not safe valid XML ({type(exc).__name__})"
            ) from None

        qr_container = next(
            (element for element in root.iter() if element.get("id") == "qr-code"),
            None,
        )
        if qr_container is None:
            raise ArtifactError("SVG template does not contain an element with id 'qr-code'")
        qr_container.text = None
        for child in tuple(qr_container):
            qr_container.remove(child)
        qr_container.append(
            _qr_svg(
                wifi_qr_payload(
                    ssid=ssid,
                    password=record.password,
                    auth_type=self.config.qr_auth_type,
                ),
                size_mm=self.config.qr_size_mm,
            )
        )

        replacements = {
            "{{SSID}}": ssid,
            "{{PASSWORD}}": record.password,
            "{{SECURITY}}": self.config.security_label,
        }
        for element in root.iter():
            element.text = _replace_placeholders(element.text, replacements)
            element.tail = _replace_placeholders(element.tail, replacements)
            for name, value in element.attrib.items():
                element.set(name, _replace_placeholders(value, replacements) or "")

        ET.register_namespace("", _SVG_NAMESPACE)
        return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def wifi_qr_payload(*, ssid: str, password: str, auth_type: str = "WPA") -> str:
    return f"WIFI:T:{auth_type};S:{_escape_wifi_value(ssid)};P:{_escape_wifi_value(password)};;"


def cleanup_old_artifacts(output_directory: Path, *, current_period: YearMonth) -> list[Path]:
    keep = {str(current_period), str(current_period.next())}
    removed: list[Path] = []
    if not output_directory.exists():
        return removed

    for path in output_directory.iterdir():
        match = _MANAGED_ARTIFACT_PATTERN.fullmatch(path.name)
        if match is None or match.group("period") in keep:
            continue
        if path.is_symlink():
            raise ArtifactError(f"refusing to remove symbolic-link artifact: {path.name}")
        if not path.is_file():
            continue
        path.unlink()
        removed.append(path)
    return removed


def _qr_svg(payload: str, *, size_mm: int) -> ET.Element:
    try:
        qr = qrcode.QRCode(
            version=None,
            error_correction=ERROR_CORRECT_Q,
            box_size=1,
            border=4,
        )
        qr.add_data(payload, optimize=0)
        qr.make(fit=True)
        matrix = qr.get_matrix()
    except Exception as exc:
        raise ArtifactError(f"QR code generation failed ({type(exc).__name__})") from None

    dimension = len(matrix)
    svg = ET.Element(
        f"{{{_SVG_NAMESPACE}}}svg",
        {
            "x": "0",
            "y": "0",
            # The poster's viewBox uses millimetres as user units. Physical "mm"
            # units here would be converted to CSS pixels a second time by Cairo.
            "width": str(size_mm),
            "height": str(size_mm),
            "viewBox": f"0 0 {dimension} {dimension}",
            "shape-rendering": "crispEdges",
        },
    )
    ET.SubElement(
        svg,
        f"{{{_SVG_NAMESPACE}}}rect",
        {"width": str(dimension), "height": str(dimension), "fill": "white"},
    )
    path_data = "".join(
        f"M{column} {row}h1v1h-1z"
        for row, values in enumerate(matrix)
        for column, dark in enumerate(values)
        if dark
    )
    ET.SubElement(
        svg,
        f"{{{_SVG_NAMESPACE}}}path",
        {"d": path_data, "fill": "black"},
    )
    return svg


def _replace_placeholders(value: str | None, replacements: dict[str, str]) -> str | None:
    if value is None:
        return None
    for placeholder, replacement in replacements.items():
        value = value.replace(placeholder, replacement)
    return value


def _escape_wifi_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\")
    for character in (";", ",", ":"):
        escaped = escaped.replace(character, f"\\{character}")
    return escaped


def _temporary_path(directory: Path, target_name: str) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=f".{target_name}.", dir=directory)
    os.close(descriptor)
    return Path(name)


def _is_nonempty_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink() and path.stat().st_size > 0


def _validate_generated_file(path: Path, *, expected: str) -> None:
    try:
        prefix = path.read_bytes()[:8]
    except OSError:
        raise ArtifactError(f"cannot validate generated {expected.upper()} file") from None
    if expected == "png" and prefix != b"\x89PNG\r\n\x1a\n":
        raise ArtifactError("generated PNG file has an invalid signature")
    if expected == "pdf" and not prefix.startswith(b"%PDF-"):
        raise ArtifactError("generated PDF file has an invalid signature")


def _cairosvg_png(**kwargs: Any) -> Any:
    try:
        from cairosvg import svg2png
    except Exception as exc:
        raise ArtifactError(f"CairoSVG PNG runtime is unavailable ({type(exc).__name__})") from None
    return svg2png(**kwargs)


def _cairosvg_pdf(**kwargs: Any) -> Any:
    try:
        from cairosvg import svg2pdf
    except Exception as exc:
        raise ArtifactError(f"CairoSVG PDF runtime is unavailable ({type(exc).__name__})") from None
    return svg2pdf(**kwargs)
