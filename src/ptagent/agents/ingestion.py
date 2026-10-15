"""Ingestion and analysis agent: scans a project and builds the shared index."""
from __future__ import annotations

from ..tools.analyzer import build_index
from .base import Agent


class IngestionAgent(Agent):
    name = "ingestion_analysis"

    def __init__(self, trace, cfg):
        super().__init__(trace)
        self.cfg = cfg

    def run(self, project_dir):
        index = build_index(project_dir, skip_dirs=self.cfg.skip_dirs)
        self.log("indexed project", **index.summary())
        return index
