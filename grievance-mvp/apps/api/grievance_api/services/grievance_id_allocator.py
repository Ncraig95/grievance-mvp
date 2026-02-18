from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..core.config import AppConfig
from ..db.db import Db
from .sharepoint_graph import GraphUploader


_FOLDER_NAME_RE = re.compile(r"^(?P<year>\d{4})(?P<seq>\d{3,})\b")


class GrievanceIdAllocationError(RuntimeError):
    pass


@dataclass(frozen=True)
class GrievanceIdAllocation:
    grievance_id: str
    year: int
    sequence: int
    sharepoint_max_seq: int
    case_folder_name: str
    case_folder_web_url: str | None


def parse_case_folder_identifier(folder_name: str) -> tuple[int, int] | None:
    match = _FOLDER_NAME_RE.match(folder_name.strip())
    if not match:
        return None
    return int(match.group("year")), int(match.group("seq"))


def max_sequence_for_year(folder_names: list[str], *, year: int) -> int:
    max_seq = 0
    for folder_name in folder_names:
        parsed = parse_case_folder_identifier(folder_name)
        if parsed is None:
            continue
        parsed_year, seq = parsed
        if parsed_year != year:
            continue
        if seq > max_seq:
            max_seq = seq
    return max_seq


def current_year_in_timezone(timezone_name: str) -> int:
    try:
        tz = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise GrievanceIdAllocationError(f"invalid timezone configured: {timezone_name}") from exc
    return datetime.now(tz).year


def format_grievance_id(*, year: int, sequence: int, min_width: int, separator: str = "") -> str:
    width = max(max(1, int(min_width)), len(str(sequence)))
    seq_text = f"{sequence:0{width}d}"
    return f"{year}{separator}{seq_text}"


class GrievanceIdAllocator:
    def __init__(
        self,
        *,
        cfg: AppConfig,
        db: Db,
        graph: GraphUploader,
        logger: logging.Logger,
    ):
        self.cfg = cfg
        self.db = db
        self.graph = graph
        self.logger = logger

    async def allocate_and_reserve_folder(
        self,
        *,
        member_name: str,
        correlation_id: str,
    ) -> GrievanceIdAllocation:
        year = current_year_in_timezone(self.cfg.grievance_id.timezone)

        try:
            folder_names = self.graph.list_case_folder_names(
                site_hostname=self.cfg.graph.site_hostname,
                site_path=self.cfg.graph.site_path,
                library=self.cfg.graph.document_library,
                case_parent_folder=self.cfg.graph.case_parent_folder,
            )
        except Exception as exc:
            raise GrievanceIdAllocationError("sharepoint folder listing unavailable") from exc

        sharepoint_max_seq = max_sequence_for_year(folder_names, year=year)
        next_seq = await self.db.reserve_next_grievance_seq(year=year, floor_seq=sharepoint_max_seq)
        grievance_id = format_grievance_id(
            year=year,
            sequence=next_seq,
            min_width=self.cfg.grievance_id.min_width,
            separator=self.cfg.grievance_id.separator,
        )

        try:
            case_folder = self.graph.ensure_case_folder(
                site_hostname=self.cfg.graph.site_hostname,
                site_path=self.cfg.graph.site_path,
                library=self.cfg.graph.document_library,
                case_parent_folder=self.cfg.graph.case_parent_folder,
                grievance_id=grievance_id,
                member_name=member_name,
            )
        except Exception as exc:
            self.logger.exception(
                "grievance_id_allocation_failed",
                extra={"correlation_id": correlation_id, "grievance_id": grievance_id, "year": year},
            )
            raise GrievanceIdAllocationError("sharepoint folder reservation failed") from exc

        return GrievanceIdAllocation(
            grievance_id=grievance_id,
            year=year,
            sequence=next_seq,
            sharepoint_max_seq=sharepoint_max_seq,
            case_folder_name=case_folder.folder_name,
            case_folder_web_url=case_folder.web_url,
        )
