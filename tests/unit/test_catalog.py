"""Catalog size, ordering, and overflow behavior."""

from agenthicc.agents.catalog import CATALOG_LIMIT, CatalogEntry, render_capability_catalog


def test_catalog_budget_preserves_matching_skills_and_new_tools() -> None:
    workflows = [
        CatalogEntry(
            "workflow", f"workflow-{index:02}", "Review the current project.", builtin=True
        )
        for index in range(60)
    ]
    skills = [
        CatalogEntry("skill", f"skill-{index:03}", "Reference for another task.")
        for index in range(119)
    ]
    skills.append(CatalogEntry("skill", "release-notes", "Draft a changelog.", ("releases",)))
    new_tool = CatalogEntry("tool", "fetch_rss", new=True)

    entries = [*workflows, *skills, new_tool]
    catalog = render_capability_catalog(
        entries, recent_text="Prepare the release notes.", used_workflows={"workflow-00"}
    )
    assert len(catalog) <= CATALOG_LIMIT
    assert "workflow-00" not in catalog
    assert "release-notes" in catalog
    assert "fetch_rss [new this session]" in catalog
    assert catalog.splitlines()[-1].endswith("more (list_workflows / list_skills)")
    assert catalog == render_capability_catalog(
        reversed(entries), recent_text="Prepare the release notes.", used_workflows={"workflow-00"}
    )
