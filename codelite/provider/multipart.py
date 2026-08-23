# Copyright 2026 Code Lite contributors
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

"""Minimal `multipart/form-data` decoder, stdlib-only.

The stdlib's old `cgi.FieldStorage` did this job but is deprecated (and
removed as of Python 3.13), so Code Lite parses the handful of fields
`/v1/images/edits` needs (a prompt, a model, and 1-5 image files) by hand
instead of depending on it.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MultipartPart:
    name: str
    filename: str | None
    content_type: str | None
    data: bytes

    @property
    def is_file(self) -> bool:
        return self.filename is not None

    def text(self) -> str:
        return self.data.decode("utf-8", errors="replace")


def parse_content_type_boundary(content_type: str) -> str | None:
    for chunk in content_type.split(";"):
        chunk = chunk.strip()
        if chunk.lower().startswith("boundary="):
            value = chunk[len("boundary=") :].strip()
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            return value
    return None


def _parse_part_headers(raw_headers: bytes) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in raw_headers.split(b"\r\n"):
        if not line:
            continue
        text = line.decode("utf-8", errors="replace")
        if ":" not in text:
            continue
        key, value = text.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    return headers


def _parse_content_disposition(value: str) -> tuple[str | None, str | None]:
    name: str | None = None
    filename: str | None = None
    for chunk in value.split(";"):
        chunk = chunk.strip()
        if chunk.startswith("name="):
            name = chunk[len("name=") :].strip('"')
        elif chunk.startswith("filename="):
            filename = chunk[len("filename=") :].strip('"')
    return name, filename


def parse_multipart(content_type: str, body: bytes) -> list[MultipartPart]:
    boundary = parse_content_type_boundary(content_type)
    if not boundary:
        raise ValueError("multipart/form-data request is missing a boundary.")

    delimiter = b"--" + boundary.encode("utf-8")
    parts: list[MultipartPart] = []

    # Body is delimiter-separated; the final segment after the closing
    # delimiter (`--boundary--`) is discarded implicitly since it never
    # matches the `name=` extraction below.
    for raw_segment in body.split(delimiter):
        segment = raw_segment
        if segment in (b"", b"--", b"--\r\n") or segment.startswith(b"--"):
            continue
        if segment.startswith(b"\r\n"):
            segment = segment[2:]
        if segment.endswith(b"\r\n"):
            segment = segment[:-2]

        header_end = segment.find(b"\r\n\r\n")
        if header_end == -1:
            continue
        headers = _parse_part_headers(segment[:header_end])
        data = segment[header_end + 4 :]

        disposition = headers.get("content-disposition", "")
        name, filename = _parse_content_disposition(disposition)
        if not name:
            continue

        parts.append(
            MultipartPart(
                name=name,
                filename=filename,
                content_type=headers.get("content-type"),
                data=data,
            )
        )

    return parts


class MultipartForm:
    """Convenience wrapper mirroring the bits of Web `FormData` the reference uses."""

    def __init__(self, parts: list[MultipartPart]) -> None:
        self._parts = parts

    def get_text(self, name: str) -> str | None:
        for part in self._parts:
            if part.name == name and not part.is_file:
                return part.text()
        return None

    def get_all_files(self, *names: str) -> list[MultipartPart]:
        return [part for part in self._parts if part.name in names and part.is_file]

    def has(self, name: str) -> bool:
        return any(part.name == name for part in self._parts)
