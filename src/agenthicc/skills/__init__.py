"""Skills system — curated tool bundles for domain-specific capabilities (PRD-18)."""

from __future__ import annotations

import abc
import logging

__all__ = ["SkillBundle", "SkillRegistry"]

logger = logging.getLogger(__name__)

_BUILTIN: dict[str, type] = {}


def _register_builtin(cls: type) -> type:
    _BUILTIN[cls().name] = cls
    return cls


class SkillBundle(abc.ABC):
    @property
    @abc.abstractmethod
    def name(self) -> str: ...
    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def description(self) -> str:
        return ""

    @property
    def required_config(self) -> list[str]:
        return []

    @abc.abstractmethod
    def tools(self, config: dict[str, object]) -> list[object]: ...
    def commands(self) -> list[object]:
        return []

    def system_prompt_addition(self) -> str:
        return ""


class SkillRegistry:
    def __init__(self) -> None:
        self._loaded: dict[str, SkillBundle] = {}
        self._tools: list[object] = []
        self._prompt_additions: list[str] = []

    def load(self, name: str, config: dict[str, object] | None = None) -> bool:
        cls = _BUILTIN.get(name)
        if cls is None:
            logger.warning("Unknown skill: %r", name)
            return False
        try:
            bundle = cls()
            missing = [k for k in bundle.required_config if not (config or {}).get(k)]
            if missing:
                logger.warning("Skill %r missing required config: %s", name, missing)
                return False
            loaded_tools = bundle.tools(config or {})
            self._loaded[name] = bundle
            self._tools.extend(loaded_tools)
            addition = bundle.system_prompt_addition()
            if addition:
                self._prompt_additions.append(addition)
            return True
        except Exception as exc:
            logger.warning("Skill %r failed to load: %s", name, exc)
            return False

    def load_all(
        self,
        names: list[str],
        configs: dict[str, dict[str, object]] | None = None,
    ) -> None:
        for name in names:
            self.load(name, (configs or {}).get(name))

    @property
    def tools(self) -> list[object]:
        return list(self._tools)

    @property
    def system_prompt_suffix(self) -> str:
        if not self._prompt_additions:
            return ""
        return "\n\n## Available Skill Capabilities\n" + "\n".join(self._prompt_additions)


@_register_builtin
class WebSearchSkill(SkillBundle):
    @property
    def name(self) -> str:
        return "web_search"

    @property
    def required_config(self) -> list[str]:
        return ["api_key"]

    def system_prompt_addition(self) -> str:
        return (
            "**Web search**: use `search_web(query)` to search and `fetch_page(url)` to read pages."
        )

    def tools(self, config: dict[str, object]) -> list[object]:
        from agenthicc.skills.web_search import FetchPageTool, SearchWebTool  # noqa: PLC0415

        raw_api_key = config.get("api_key", "")
        api_key = raw_api_key if isinstance(raw_api_key, str) else ""
        raw_engine = config.get("engine", "brave")
        engine = raw_engine if isinstance(raw_engine, str) else "brave"
        raw_max_results = config.get("max_results", 5)
        max_results = raw_max_results if isinstance(raw_max_results, int) else 5
        return [
            SearchWebTool(
                api_key=str(api_key),
                engine=str(engine),
                max_results=max_results,
            ),
            FetchPageTool(),
        ]


@_register_builtin
class DockerSkill(SkillBundle):
    @property
    def name(self) -> str:
        return "docker"

    def system_prompt_addition(self) -> str:
        return (
            "**Docker**: manage containers with `run_bash('docker ...')` or dedicated docker tools."
        )

    def tools(self, config: dict[str, object]) -> list[object]:
        return []  # placeholder — docker tools use run_command under the hood


@_register_builtin
class DatabaseSkill(SkillBundle):
    @property
    def name(self) -> str:
        return "database"

    @property
    def required_config(self) -> list[str]:
        return ["connection_string"]

    def system_prompt_addition(self) -> str:
        return (
            "**Database**: query with `db_query(sql)`, inspect with `db_schema`/`db_list_tables`."
        )

    def tools(self, config: dict[str, object]) -> list[object]:
        return []  # placeholder — actual DB tools require asyncpg/sqlite3 drivers
