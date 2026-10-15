"""Agent base class and a shared trace that records what every agent did."""
from __future__ import annotations


class Trace:
    def __init__(self):
        self.events = []

    def add(self, agent: str, action: str, **detail):
        self.events.append({"step": len(self.events) + 1, "agent": agent, "action": action, **detail})


class Agent:
    name = "agent"

    def __init__(self, trace: Trace):
        self.trace = trace

    def log(self, action: str, **detail):
        self.trace.add(self.name, action, **detail)
