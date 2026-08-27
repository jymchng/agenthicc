"""Behavioral coverage for the website reconstruction workflows.

These tests deliberately drive the real phase transition closures.  A fake
``run_phase`` stands in for the model turn, but the workflow runners, context
mutation, phase loops, and transition-tool contracts are exercised unchanged.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from typing import Any

import pytest

from agenthicc.workflows import name_that_ui
from agenthicc.workflows.make_agenthicc_tool.runner import (
    CACHE_CONTRACT as TOOL_CACHE_CONTRACT,
    MakeAgenthiccToolWorkflow,
    MakeToolContext,
    MakeToolRunner,
    MakeToolState,
)
from agenthicc.workflows.copy_website.runner import (
    CACHE_CONTRACT as COPY_CACHE_CONTRACT,
    CopyContext,
    CopyWebsiteRunner,
    CopyWebsiteState,
    CopyWebsiteWorkflow,
    _make_data_tools as make_copy_data_tools,
    _make_design_tools as make_copy_design_tools,
    _make_extract_tools as make_copy_extract_tools,
    _make_layout_tools as make_copy_layout_tools,
    _make_pages_tools as make_copy_pages_tools,
    _make_parity_tools as make_copy_parity_tools,
    _make_responsive_tools as make_copy_responsive_tools,
    _make_scaffold_tools as make_copy_scaffold_tools,
    _make_study_tools as make_copy_study_tools,
)
from agenthicc.workflows.reconstruct_site.runner import (
    CACHE_CONTRACT as RECONSTRUCT_CACHE_CONTRACT,
    ReconstructContext,
    ReconstructSiteRunner,
    ReconstructState,
)
from agenthicc.workflows.make_epub_book.runner import (
    ChapterInfo as EpubChapterInfo,
    MakeEpubBookContext,
    MakeEpubBookRunner,
    MakeEpubBookState,
    MakeEpubBookWorkflow,
    _make_assets_tools as make_epub_assets_tools,
    _make_back_matter_tools as make_epub_back_matter_tools,
    _make_chapter_tools as make_epub_chapter_tools,
    _make_compile_tools as make_epub_compile_tools,
    _make_front_matter_tools as make_epub_front_matter_tools,
    _make_research_tools as make_epub_research_tools,
    _make_toc_tools as make_epub_toc_tools,
)
from agenthicc.workflows.make_pdf_book.runner import (
    ChapterInfo as PdfChapterInfo,
    MakePdfBookContext,
    MakePdfBookRunner,
    MakePdfBookState,
    MakePdfBookWorkflow,
    _make_assets_tools as make_pdf_assets_tools,
    _make_back_matter_tools as make_pdf_back_matter_tools,
    _make_chapter_tools as make_pdf_chapter_tools,
    _make_compile_tools as make_pdf_compile_tools,
    _make_front_matter_tools as make_pdf_front_matter_tools,
    _make_research_tools as make_pdf_research_tools,
    _make_toc_tools as make_pdf_toc_tools,
)
from agenthicc.workflows.site_imitate.runner import (
    MOBILE_RESPONSIVE_CONTRACT,
    SiteImitateContext,
    SiteImitateRunner,
    SiteImitateState,
    _component_plan_text,
    _parse_component_plan,
    _make_analyze_tools as make_imitate_analyze_tools,
    _make_build_tools as make_imitate_build_tools,
    _make_final_verify_tools as make_imitate_final_tools,
    _make_plan_tools as make_imitate_plan_tools,
    _make_scaffold_tools as make_imitate_scaffold_tools,
    _make_verify_tools as make_imitate_verify_tools,
)

pytestmark = pytest.mark.unit


def _runner_config() -> SimpleNamespace:
    return SimpleNamespace(
        workflow_handle=None,
        session_memory=object(),
        cfg=SimpleNamespace(
            execution=SimpleNamespace(effective_usable_budget=lambda: 10_000),
        ),
    )


def _runner(cls: type[Any]) -> Any:
    runner = object.__new__(cls)
    runner._cfg = _runner_config()
    return runner


def _value(tool_name: str, parameter: str) -> object:
    """Return a valid deterministic value for a transition-tool parameter."""
    if parameter == "chapters":
        return [{"title": "Chapter One", "outline": "An outline"}]
    if parameter == "notes":
        return [{"chapter_index": 0, "notes": "Authoritative research notes."}]
    if parameter == "sources":
        return ["https://source.test/reference"]
    if parameter == "content_types":
        return ["images", "code"]
    if parameter == "technical_level":
        return "advanced"
    if parameter == "chapter_index":
        return 0
    if parameter == "word_count":
        return 500
    if parameter in {"pdf_path", "epub_path"}:
        return "book.pdf" if parameter == "pdf_path" else "book.epub"
    if parameter == "files" and tool_name in {
        "confirm_front_matter_ready",
        "confirm_back_matter_ready",
    }:
        return ["book.md"]
    if parameter in {"reference_url", "url", "target_url"}:
        return "https://example.test"
    if parameter in {"target_directory", "project_path", "path"}:
        return "/tmp/generated-site"
    if parameter == "file_path":
        return ".agenthicc/tools/demo.py"
    if parameter in {"constraints", "scope", "desired_routes", "pages"}:
        return "/, /about"
    if parameter == "components" and tool_name in {
        "submit_plan",
    }:
        return ["Header | responsive header | renders at mobile and desktop"]
    if parameter in {"report", "spec", "tokens", "components", "data_plan"}:
        return "observed and verified"
    if parameter in {
        "files",
        "pages_built",
        "hooks",
        "verified",
        "summary",
        "reason",
        "errors",
        "issue",
        "architecture",
        "description",
    }:
        if parameter == "pages_built":
            return "/, /about"
        return "verified mobile responsive behavior at every viewport"
    if parameter == "routes":
        return [{"route": "/", "purpose": "home"}]
    if parameter == "interactions":
        return [{"interaction": "navigation", "trigger": "click"}]
    if parameter == "assets":
        if tool_name == "submit_asset_inventory":
            return [{"name": "logo", "type": "svg"}]
        return ["assets/logo.svg"]
    if parameter in {"design_tokens", "component_map"}:
        return {"primary": "#123456"}
    if parameter in {"issues", "discrepancies"}:
        return [{"issue": "spacing", "severity": "low"}]
    if parameter == "page_route":
        return "/"
    if parameter == "question":
        return "Please re-check the navigation details."
    if parameter == "platform" or parameter == "query":
        return ""
    if parameter == "auth_required":
        return False
    if parameter == "reference_is_static":
        return True
    if parameter == "reproduce_api_or_mock":
        return "mock"
    if parameter == "target_phase":
        return ""
    if parameter == "satisfied":
        return True
    if parameter == "files":
        return ["src/app/page.tsx"]
    return "value"


async def _call_transition(tool: Any) -> object:
    kwargs: dict[str, object] = {}
    tool_name = getattr(tool, "__name__", "")
    for parameter in inspect.signature(tool).parameters.values():
        if parameter.default is inspect.Parameter.empty or parameter.name in {
            "components",
            "target_phase",
        }:
            value = _value(tool_name, parameter.name)
            if parameter.name in {"pdf_path", "epub_path"}:
                module = getattr(tool, "__module__", "")
                value = "book.epub" if "make_epub_book" in module else "book.pdf"
            kwargs[parameter.name] = value
    return await tool(**kwargs)


def _is_transition(tool_name: str) -> bool:
    return tool_name not in {"lookup_component", "list_components"} and not tool_name.endswith(
        "_failed"
    )


async def _successful_run_phase(**kwargs: object) -> None:
    tools = kwargs.get("tools", [])
    assert isinstance(tools, list)
    for tool in tools:
        name = getattr(tool, "__name__", "")
        if _is_transition(name) and name not in {
            "reject_parity",
            "visual_rejected",
            "interaction_rejected",
            "a11y_rejected",
            "perf_rejected",
            "fidelity_rejected",
        }:
            await _call_transition(tool)
            return


def _reconstruct_context() -> ReconstructContext:
    return ReconstructContext(
        intent="reconstruct https://example.test",
        run_id="reconstruct-run",
        state=ReconstructState.INIT,
        target_url="https://example.test",
        target_directory="/tmp/generated-site",
        pages_to_implement=["/"],
        route_inventory=[{"route": "/", "purpose": "home"}],
        design_tokens={"primary": "#123456"},
        architecture="Next.js App Router",
        interaction_inventory=[{"interaction": "navigation"}],
        component_inventory=[{"name": "Header"}],
        asset_inventory=[{"name": "logo"}],
        shared_memory=object(),
    )


@pytest.mark.asyncio
async def test_copy_website_full_driver_and_checkpoint_codec() -> None:
    runner = _runner(CopyWebsiteRunner)
    runner.run_phase = _successful_run_phase  # type: ignore[method-assign]

    context = await runner.run("copy https://example.test")
    assert context.state is CopyWebsiteState.COMPLETE
    assert context.target_url == "https://example.test"
    assert context.target_pages == ["/", "/about"]
    assert context.pages_built == ["/", "/about"]
    assert context.parity_summary

    payload = CopyWebsiteWorkflow.checkpoint_context_to_payload(context)
    restored = CopyWebsiteWorkflow.checkpoint_context_from_payload(payload, memory="memory")
    assert restored.state is CopyWebsiteState.COMPLETE
    assert restored.shared_memory == "memory"
    assert restored.artifacts == context.artifacts


@pytest.mark.asyncio
async def test_copy_website_resume_from_extract_and_checkpoint_guards() -> None:
    runner = _runner(CopyWebsiteRunner)
    runner.run_phase = _successful_run_phase  # type: ignore[method-assign]
    resumed = await runner.resume(
        CopyContext(
            intent="copy https://example.test",
            run_id="resume",
            state=CopyWebsiteState.EXTRACT_TARGET,
        )
    )
    assert resumed.state is CopyWebsiteState.COMPLETE
    with pytest.raises(TypeError):
        await runner.resume(object())
    with pytest.raises(TypeError):
        CopyWebsiteWorkflow.checkpoint_context_to_payload(object())


@pytest.mark.asyncio
async def test_site_imitate_full_dynamic_component_driver() -> None:
    runner = _runner(SiteImitateRunner)
    runner.run_phase = _successful_run_phase  # type: ignore[method-assign]

    context = await runner.run("https://example.test | a mobile dashboard")
    assert context.state is SiteImitateState.COMPLETE
    assert context.target_url == "https://example.test"
    assert context.new_purpose == "a mobile dashboard"
    assert context.component_plan
    assert context.artifacts["verify_summary"]
    assert runner.total_phases == 6


@pytest.mark.asyncio
async def test_site_imitate_dictionary_tools_and_soft_catalog_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agenthicc.workflows.site_imitate.runner as module

    record = {
        "name": "Drawer",
        "platform": "web",
        "api": [{"framework": "shadcn/ui", "symbol": "Sheet"}],
    }
    ntui = SimpleNamespace(
        lookup=lambda _description, _catalog, top=5: [record][:top],
        list_names=lambda _catalog, platform="", query="": [{"name": "Drawer"}],
        match_brief=lambda item: {"name": item["name"]},
        format_matches=lambda _matches: "Drawer (Sheet)",
        format_name_list=lambda _names: "[web] (1):\nDrawer",
        load_catalog=lambda: [record],
    )
    monkeypatch.setattr(module, "_import_ntui", lambda: ntui)
    assert module._load_ntui_catalog() == [record]

    event = asyncio.Event()
    data: dict[str, str] = {}
    analyze_tools = make_imitate_analyze_tools(event, data, [record])
    list_components, lookup, submit = analyze_tools
    assert (await lookup("drawer"))["matches"] == [{"name": "Drawer"}]
    assert (await list_components(platform="web", query="drawer"))["total"] == 1
    assert (await submit("", ""))["ok"] is False
    assert (await submit("analysis", "inventory"))["ok"] is True

    build_event = asyncio.Event()
    build_data: dict[str, str] = {}
    build_lookup, built = make_imitate_build_tools(build_event, build_data, [record])
    assert (await build_lookup("drawer"))["matches"] == [{"name": "Drawer"}]
    assert (await built())["ok"] is True

    monkeypatch.setattr(module, "_import_ntui", lambda: None)
    assert module._load_ntui_catalog() == []
    unavailable_lookup = make_imitate_analyze_tools(asyncio.Event(), {}, [])[0]
    assert (await unavailable_lookup("drawer"))["ok"] is False
    unavailable_build_lookup = make_imitate_build_tools(asyncio.Event(), {}, [])[0]
    assert (await unavailable_build_lookup("drawer"))["ok"] is False

    monkeypatch.setattr(
        module,
        "_import_ntui",
        lambda: SimpleNamespace(load_catalog=lambda: (_ for _ in ()).throw(OSError("bad cache"))),
    )
    assert module._load_ntui_catalog() == []


@pytest.mark.asyncio
async def test_site_imitate_plan_reanalysis_and_failure_branches() -> None:
    runner = _runner(SiteImitateRunner)
    ctx = SiteImitateContext(intent="url | purpose", analysis="observations")

    async def request_reanalysis(**kwargs: object) -> None:
        tool = next(
            tool
            for tool in kwargs["tools"]
            if getattr(tool, "__name__", "") == "request_reanalysis"
        )  # type: ignore[index]
        await tool("need more details")  # type: ignore[operator]

    runner.run_phase = request_reanalysis  # type: ignore[method-assign]
    assert await runner._plan(ctx, object()) is SiteImitateState.ANALYZE
    assert ctx.artifacts["reanalysis_question"] == "need more details"

    event = asyncio.Event()
    data: dict[str, str] = {}
    scaffold = make_imitate_scaffold_tools(event, data)[0]
    assert (await scaffold("/tmp/site"))["ok"] is True
    runner.run_phase = _successful_run_phase  # type: ignore[method-assign]
    empty_ctx = SiteImitateContext(intent="url", plan="plan")
    assert await runner._scaffold(empty_ctx, object()) is SiteImitateState.FINAL_VERIFY

    ctx.component_plan = [{"name": "Header", "build": "", "verify": ""}]
    ctx.component_index = 0

    async def component_failed(**kwargs: object) -> None:
        tool = next(
            tool
            for tool in kwargs["tools"]
            if getattr(tool, "__name__", "") == "component_verification_failed"
        )  # type: ignore[index]
        await tool("type error")  # type: ignore[operator]

    runner.run_phase = component_failed  # type: ignore[method-assign]
    assert await runner._verify_component(ctx, object()) is SiteImitateState.BUILD
    assert ctx.artifacts["verify_errors_0"] == "type error"

    async def final_failed(**kwargs: object) -> None:
        tool = next(
            tool
            for tool in kwargs["tools"]
            if getattr(tool, "__name__", "") == "final_verify_failed"
        )  # type: ignore[index]
        await tool("build error")  # type: ignore[operator]

    runner.run_phase = final_failed  # type: ignore[method-assign]
    assert await runner._final_verify(ctx, object()) is SiteImitateState.BUILD
    assert ctx.component_index == 0
    assert ctx.artifacts["verify_errors"] == "build error"

    assert (
        _parse_component_plan(["", "Header", " | build | verify", "Footer | build | verify"])[-1][
            "name"
        ]
        == "Footer"
    )
    assert _parse_component_plan(None) == []
    assert "no components" in _component_plan_text(SiteImitateContext(intent="url"))
    assert "Header" in _component_plan_text(ctx)
    assert SiteImitateRunner._phase_index(SiteImitateState.COMPLETE, ctx) == 0


def test_site_imitate_checkpoint_and_parameters() -> None:
    from agenthicc.workflows.site_imitate.runner import SiteImitateParams, SiteImitateWorkflow

    context = SiteImitateContext(
        intent="url | purpose",
        target_url="url",
        new_purpose="purpose",
        run_id="run",
        state=SiteImitateState.COMPLETE,
        component_plan=[{"name": "Header", "build": "", "verify": ""}],
        artifacts={"plan": "done"},
    )
    payload = SiteImitateWorkflow.checkpoint_context_to_payload(context)
    restored = SiteImitateWorkflow.checkpoint_context_from_payload(payload, memory="memory")
    assert restored.state is SiteImitateState.COMPLETE
    assert restored.component_plan[0]["name"] == "Header"
    assert restored.shared_memory == "memory"
    payload["state"] = "bad"
    with pytest.raises(ValueError, match="unknown site_imitate state"):
        SiteImitateWorkflow.checkpoint_context_from_payload(payload)
    params = SiteImitateParams(analyze_model="analyze", final_verify_model="final")
    assert params.get_phase_models()["analyze"] == "analyze"
    assert (
        SiteImitateWorkflow.build_params({"build_model": "builder"}).get_phase_models()["build"]
        == "builder"
    )


def test_site_imitate_phase_indexes_and_resume_guards() -> None:
    from agenthicc.workflows.site_imitate.runner import SiteImitateWorkflow

    context = SiteImitateContext(intent="url", component_plan=[{"name": "Header"}])
    assert SiteImitateRunner._phase_index(SiteImitateState.ANALYZE, context) == 0
    assert SiteImitateRunner._phase_index(SiteImitateState.PLAN, context) == 1
    assert SiteImitateRunner._phase_index(SiteImitateState.SCAFFOLD, context) == 2
    assert SiteImitateRunner._phase_index(SiteImitateState.BUILD, context) == 3
    assert SiteImitateRunner._phase_index(SiteImitateState.VERIFY, context) == 4
    assert SiteImitateRunner._phase_index(SiteImitateState.FINAL_VERIFY, context) == 5
    assert SiteImitateWorkflow.name == "site_imitate"


@pytest.mark.asyncio
async def test_site_imitate_resume_from_analyze_and_remaining_failure_paths() -> None:
    runner = _runner(SiteImitateRunner)
    runner.run_phase = _successful_run_phase  # type: ignore[method-assign]
    resumed = await runner.resume(
        SiteImitateContext(
            intent="https://example.test | purpose",
            state=SiteImitateState.ANALYZE,
        )
    )
    assert resumed.state is SiteImitateState.COMPLETE

    async def no_transition(**_kwargs: object) -> None:
        return None

    runner.run_phase = no_transition  # type: ignore[method-assign]
    for method, state in (
        ("_analyze", SiteImitateState.ANALYZE),
        ("_plan", SiteImitateState.PLAN),
        ("_scaffold", SiteImitateState.SCAFFOLD),
        ("_build_component", SiteImitateState.BUILD),
        ("_verify_component", SiteImitateState.VERIFY),
        ("_final_verify", SiteImitateState.FINAL_VERIFY),
    ):
        context = SiteImitateContext(
            intent="url",
            state=state,
            analysis="analysis",
            plan="plan",
            component_plan=[{"name": "Header", "build": "", "verify": ""}],
        )
        assert await getattr(runner, method)(context, object()) is SiteImitateState.FAILED
        assert context.fail_reason

    with pytest.raises(TypeError, match="SiteImitateContext"):
        await runner.resume(object())


@pytest.mark.asyncio
async def test_reconstruct_site_full_driver_covers_dynamic_page_and_infrastructure() -> None:
    runner = _runner(ReconstructSiteRunner)
    runner.run_phase = _successful_run_phase  # type: ignore[method-assign]

    context = await runner.run("reconstruct https://example.test")
    assert context.state is ReconstructState.COMPLETE
    assert context.page_index == 1
    assert context.implementation_status["/"] == "implemented"
    assert context.infra_status["sqlite"] == "verified"
    assert context.infra_status["docs"] == "verified"
    assert context.validation_status["final"] == "approved"
    assert "final_validation" in context.completed_phases


@pytest.mark.asyncio
async def test_reconstruct_site_resume_dispatches_every_declared_phase() -> None:
    runner = _runner(ReconstructSiteRunner)
    runner.run_phase = _successful_run_phase  # type: ignore[method-assign]
    resumed = await runner.resume(_reconstruct_context())
    assert resumed.state is ReconstructState.COMPLETE
    with pytest.raises(TypeError, match="ReconstructContext"):
        await runner.resume(object())


@pytest.mark.asyncio
async def test_make_agenthicc_tool_full_driver_and_repair_loop() -> None:
    runner = _runner(MakeToolRunner)
    calls = 0

    async def run_phase_with_one_validation_rejection(**kwargs: object) -> None:
        nonlocal calls
        tools = kwargs.get("tools", [])
        assert isinstance(tools, list)
        names = {getattr(tool, "__name__", "") for tool in tools}
        if "approve_tool" in names:
            calls += 1
            if calls == 1:
                reject = next(
                    tool for tool in tools if getattr(tool, "__name__", "") == "reject_tool"
                )
                await reject("missing capability tag")  # type: ignore[operator]
            else:
                approve = next(
                    tool for tool in tools if getattr(tool, "__name__", "") == "approve_tool"
                )
                await approve("imports, schema, and capabilities verified")  # type: ignore[operator]
            return
        await _successful_run_phase(**kwargs)

    runner.run_phase = run_phase_with_one_validation_rejection  # type: ignore[method-assign]
    context = await runner.run("create a project status tool")
    assert context.state is MakeToolState.COMPLETE
    assert context.tool_file_path == ".agenthicc/tools/demo.py"
    assert context.validation_report
    assert calls == 2
    assert "validation" in TOOL_CACHE_CONTRACT.lower()

    payload = MakeAgenthiccToolWorkflow.checkpoint_context_to_payload(context)
    restored = MakeAgenthiccToolWorkflow.checkpoint_context_from_payload(payload, memory="memory")
    assert restored.state is MakeToolState.COMPLETE
    assert restored.shared_memory == "memory"


@pytest.mark.asyncio
async def test_make_agenthicc_tool_transition_validation_and_bounded_failure() -> None:
    from agenthicc.workflows.make_agenthicc_tool.runner import (
        _make_analyze_tools,
        _make_generate_tools,
        _make_validate_tools,
    )

    event = asyncio.Event()
    data: dict[str, object] = {}
    analyze = _make_analyze_tools(event, data)[0]
    assert (await analyze(tool_name="", description=""))["ok"] is False
    assert (await analyze(tool_name="bad-name", description="x"))["ok"] is False
    assert (await analyze(tool_name="good_name", description="x", capabilities=["bad"]))[
        "ok"
    ] is False
    assert (
        await analyze(
            tool_name="good_name",
            description="x",
            parameters=[{"name": "ok"}, {"name": "bad-name"}],
        )
    )["ok"] is True

    event.clear()
    generate = _make_generate_tools(event, data)[0]
    assert (await generate(file_path="tool.py", summary="x"))["ok"] is False
    assert (await generate(file_path=".agenthicc/tools/demo.py", summary=""))["ok"] is False
    assert (await generate(file_path=".agenthicc/tools/demo.py", summary="written"))["ok"] is True

    event.clear()
    approve, reject = _make_validate_tools(event, data)
    assert (await approve(""))["ok"] is False
    assert (await reject(""))["ok"] is False
    assert (await reject("fix this"))["ok"] is True

    runner = _runner(MakeToolRunner)

    async def no_transition(**_kwargs: object) -> None:
        return None

    runner.run_phase = no_transition  # type: ignore[method-assign]
    context = MakeToolContext(intent="create a tool")
    assert await runner._analyze(context, object()) is MakeToolState.FAILED


@pytest.mark.asyncio
async def test_make_agenthicc_tool_resume_and_remaining_failure_paths() -> None:
    runner = _runner(MakeToolRunner)
    runner.run_phase = _successful_run_phase  # type: ignore[method-assign]
    resumed = await runner.resume(
        MakeToolContext(intent="create a tool", state=MakeToolState.ANALYZE)
    )
    assert resumed.state is MakeToolState.COMPLETE

    async def no_transition(**_kwargs: object) -> None:
        return None

    runner.run_phase = no_transition  # type: ignore[method-assign]
    for method, state in (
        ("_generate", MakeToolState.GENERATE),
        ("_validate", MakeToolState.VALIDATE),
    ):
        context = MakeToolContext(intent="create a tool", state=state)
        assert await getattr(runner, method)(context, object()) is MakeToolState.FAILED
        assert context.fail_reason

    from agenthicc.workflows.make_agenthicc_tool.runner import MakeToolParams

    assert MakeToolParams(
        analyze_model="a", generate_model="g", validate_model="v", finalize_model="f"
    ).get_phase_models() == {
        "analyze": "a",
        "generate": "g",
        "validate": "v",
        "finalize": "f",
    }


def test_make_agenthicc_tool_checkpoint_codec_and_factory_guards() -> None:
    with pytest.raises(TypeError, match="MakeToolContext"):
        MakeAgenthiccToolWorkflow.checkpoint_context_to_payload(object())
    with pytest.raises(ValueError, match="unknown make_agenthicc_tool state"):
        MakeAgenthiccToolWorkflow.checkpoint_context_from_payload({"state": "missing"})
    restored = MakeAgenthiccToolWorkflow.checkpoint_context_from_payload(
        {
            "state": "ANALYZE",
            "parameters": [{"name": "path"}, "bad"],
            "artifacts": {"plan": 1},
        },
        memory="memory",
    )
    assert restored.shared_memory == "memory"
    assert restored.parameters[0].name == "path"
    assert restored.artifacts == {"plan": "1"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runner_cls", "state_cls", "workflow_cls", "expected_suffix"),
    [
        (MakePdfBookRunner, MakePdfBookState, MakePdfBookWorkflow, ".pdf"),
        (MakeEpubBookRunner, MakeEpubBookState, MakeEpubBookWorkflow, ".epub"),
    ],
)
async def test_book_workflows_full_dynamic_driver_and_checkpoint_codec(
    runner_cls: type[Any],
    state_cls: type[Any],
    workflow_cls: type[Any],
    expected_suffix: str,
) -> None:
    runner = _runner(runner_cls)
    runner.run_phase = _successful_run_phase  # type: ignore[method-assign]

    context = await runner.run("write a technical book")
    assert context.state is state_cls.COMPLETE
    assert len(context.chapters) == 1
    assert context.chapters[0].status == "written"
    output_path = getattr(context, "pdf_path", getattr(context, "epub_path", ""))
    assert output_path.endswith(expected_suffix)

    payload = workflow_cls.checkpoint_context_to_payload(context)
    restored = workflow_cls.checkpoint_context_from_payload(payload, memory="memory")
    assert restored.state is state_cls.COMPLETE
    assert restored.shared_memory == "memory"
    assert restored.chapters[0].title == "Chapter One"

    # ``resume`` must also accept an already-terminal checkpoint without
    # calling another model turn.
    resumed = await runner.resume(context)
    assert resumed.state is state_cls.COMPLETE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runner_cls", "context_cls", "state_cls", "workflow_cls"),
    [
        (MakePdfBookRunner, MakePdfBookContext, MakePdfBookState, MakePdfBookWorkflow),
        (MakeEpubBookRunner, MakeEpubBookContext, MakeEpubBookState, MakeEpubBookWorkflow),
    ],
)
async def test_book_workflows_resume_from_the_first_phase(
    runner_cls: type[Any],
    context_cls: type[Any],
    state_cls: type[Any],
    workflow_cls: type[Any],
) -> None:
    runner = _runner(runner_cls)
    runner.run_phase = _successful_run_phase  # type: ignore[method-assign]
    context = context_cls(intent="book", run_id="resume", state=state_cls.TOC)
    resumed = await runner.resume(context)
    assert resumed.state is state_cls.COMPLETE
    restored = workflow_cls.checkpoint_context_from_payload(
        workflow_cls.checkpoint_context_to_payload(resumed), memory="memory"
    )
    assert restored.shared_memory == "memory"


def test_book_workflow_checkpoint_and_param_guards() -> None:
    for workflow_cls, state_cls in (
        (MakePdfBookWorkflow, MakePdfBookState),
        (MakeEpubBookWorkflow, MakeEpubBookState),
    ):
        with pytest.raises(TypeError):
            workflow_cls.checkpoint_context_to_payload(object())
        with pytest.raises(ValueError, match="unknown"):
            workflow_cls.checkpoint_context_from_payload({"state": "missing"})
        restored = workflow_cls.checkpoint_context_from_payload(
            {"state": state_cls.TOC.name, "chapters": ["bad", {"title": "Chapter"}]}
        )
        assert len(restored.chapters) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module_factories",
    [
        (
            make_pdf_toc_tools,
            make_pdf_research_tools,
            make_pdf_chapter_tools,
            make_pdf_assets_tools,
            make_pdf_front_matter_tools,
            make_pdf_back_matter_tools,
            make_pdf_compile_tools,
        ),
        (
            make_epub_toc_tools,
            make_epub_research_tools,
            make_epub_chapter_tools,
            make_epub_assets_tools,
            make_epub_front_matter_tools,
            make_epub_back_matter_tools,
            make_epub_compile_tools,
        ),
    ],
)
async def test_book_transition_tools_cover_validation_and_decisions(
    module_factories: tuple[Any, ...],
) -> None:
    (
        make_toc,
        make_research,
        make_chapter,
        make_assets,
        make_front,
        make_back,
        make_compile,
    ) = module_factories

    event = asyncio.Event()
    data: dict[str, object] = {}
    toc = make_toc(event, data)[0]
    assert (await toc(title="", author="", chapters=[]))["ok"] is False
    assert (await toc(title="Book", author="Author", chapters=[], technical_level="advanced"))[
        "ok"
    ] is False
    assert (
        await toc(
            title="Book",
            author="Author",
            chapters=[{"title": "Chapter"}],
            technical_level="unknown",
        )
    )["ok"] is False
    assert (
        await toc(
            title="Book", author="Author", chapters=[{"title": "Chapter"}], content_types=["video"]
        )
    )["ok"] is False
    assert (await toc(title="Book", author="Author", chapters=[{"title": ""}, "bad"]))[
        "ok"
    ] is False
    assert (
        await toc(
            title="Book", author="Author", chapters=[{"title": "Chapter", "outline": "Outline"}]
        )
    )["ok"] is True

    event.clear()
    research = make_research(event, data, 2)[0]
    assert (await research(notes=[], sources=[], summary=""))["ok"] is False
    assert (
        await research(notes=[{"chapter_index": 0, "notes": "one"}], sources=[], summary="summary")
    )["ok"] is False
    assert (
        await research(
            notes=[{"chapter_index": 0, "notes": "one"}, {"chapter_index": 1, "notes": "two"}],
            sources=[],
            summary="summary",
        )
    )["ok"] is True

    event.clear()
    chapter = make_chapter(event, data)[0]
    assert (await chapter(chapter_index=-1, file_path="chapter.md", word_count=1))["ok"] is False
    assert (await chapter(chapter_index=0, file_path="", word_count=1))["ok"] is False
    assert (await chapter(chapter_index=0, file_path="chapter.md", word_count=-1))["ok"] is True

    event.clear()
    assets = make_assets(event, data)[0]
    assert (await assets(assets=[]))["ok"] is False
    assert (await assets(assets=["assets/figure.svg", ""]))["ok"] is True

    for factory in (make_front, make_back):
        event.clear()
        tool = factory(event, data)[0]
        assert (await tool(summary="", files=[]))["ok"] is False
        assert (await tool(summary="created", files=[]))["ok"] is False
        assert (await tool(summary="created", files=["book.md", ""]))["ok"] is True

    event.clear()
    complete, reject = make_compile(event, data)
    path_name = "epub_path" if "epub" in getattr(complete, "__module__", "") else "pdf_path"
    wrong_path = {path_name: "book.txt", "summary": "done"}
    assert (await complete(**wrong_path))["ok"] is False
    assert (
        await complete(
            **{path_name: "book.pdf" if path_name == "pdf_path" else "book.epub", "summary": ""}
        )
    )["ok"] is False
    assert (await reject(issue=""))["ok"] is False
    assert (await reject(issue="rewrite chapter", chapter_index=0))["ok"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runner_cls", "context", "methods"),
    [
        (
            MakePdfBookRunner,
            MakePdfBookContext(
                intent="book",
                chapters=[PdfChapterInfo(0, title="Chapter", outline="Outline")],
            ),
            (
                "_toc",
                "_research",
                "_assets",
                "_chapter",
                "_front_matter",
                "_back_matter",
                "_compile",
            ),
        ),
        (
            MakeEpubBookRunner,
            MakeEpubBookContext(
                intent="book",
                chapters=[EpubChapterInfo(0, title="Chapter", outline="Outline")],
            ),
            (
                "_toc",
                "_research",
                "_assets",
                "_chapter",
                "_front_matter",
                "_back_matter",
                "_compile",
            ),
        ),
    ],
)
async def test_book_phase_loops_fail_after_bounded_attempts(
    runner_cls: type[Any], context: Any, methods: tuple[str, ...]
) -> None:
    runner = _runner(runner_cls)

    async def no_transition(**_kwargs: object) -> None:
        return None

    runner.run_phase = no_transition  # type: ignore[method-assign]
    for method in methods:
        result = await getattr(runner, method)(context, object())
        assert result.name == "FAILED"
        assert context.fail_reason


def test_name_that_ui_matching_and_formatting_are_deterministic() -> None:
    records = [
        {
            "name": "Nav Drawer",
            "platform": "web",
            "tagline": "A sliding menu",
            "description": "Three lines open a side panel",
            "aka": ["hamburger menu"],
            "fuzzy": ["navigation drawer"],
            "api": [
                {"framework": "shadcn/ui", "symbol": "Sheet"},
                {"framework": "ARIA", "symbol": "aria-expanded"},
            ],
            "prompt": "Build a responsive drawer.",
        },
        {
            "name": "Date Picker",
            "platform": "web",
            "description": "Choose a date",
            "api": [],
        },
        {"platform": "web"},
    ]
    matches = name_that_ui.lookup("three lines navigation drawer", records, top=1)
    assert [record["name"] for record in matches] == ["Nav Drawer"]
    assert name_that_ui.shadcn_symbols(records[0]) == ["Sheet"]
    brief = name_that_ui.match_brief(records[0])
    assert brief["shadcn"] == ["Sheet"]
    assert "Nav Drawer" in name_that_ui.format_matches(matches)
    assert name_that_ui.inventory_line(records[0]) == "Nav Drawer (Sheet)"
    assert name_that_ui.inventory_line({"name": "Plain", "api": []}) == "Plain"

    listed = name_that_ui.list_names(records, platform="WEB", query="date", top=1)
    assert listed == [{"name": "Date Picker", "platform": "web", "shadcn": []}]
    assert "Date Picker" in name_that_ui.format_name_list(listed)
    assert "no components" in name_that_ui.format_name_list([])


def test_name_that_ui_catalog_extraction_and_soft_cache_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    escaped = r'{"slug":"drawer","name":"Drawer","description":"A \\"panel\\""}'
    html = f'<script>self.__next_f.push([1,"{escaped}"])</script>'
    monkeypatch.setattr(name_that_ui, "_fetch_html", lambda: html)
    records = name_that_ui.fetch_catalog()
    assert records[0]["slug"] == "drawer"

    cache = tmp_path / "catalog.json"
    cache.write_text('[{"name": "Cached"}]', encoding="utf-8")
    assert name_that_ui.load_catalog(str(cache), ttl=60)[0]["name"] == "Cached"
    monkeypatch.setattr(name_that_ui, "fetch_catalog", lambda: [{"name": "Fresh"}])
    cache.unlink()
    assert name_that_ui.load_catalog(str(cache))[0]["name"] == "Fresh"
    monkeypatch.setattr(
        name_that_ui, "fetch_catalog", lambda: (_ for _ in ()).throw(OSError("offline"))
    )
    cache.unlink(missing_ok=True)
    assert name_that_ui.load_catalog(str(cache)) == []


@pytest.mark.asyncio
async def test_website_transition_factories_reject_empty_required_values() -> None:
    factories = [
        (make_copy_extract_tools, ()),
        (make_copy_study_tools, ()),
        (make_copy_design_tools, ()),
        (make_copy_scaffold_tools, ()),
        (make_copy_layout_tools, ()),
        (make_copy_pages_tools, ()),
        (make_copy_data_tools, ()),
        (make_copy_responsive_tools, ()),
        (make_imitate_analyze_tools, ([],)),
        (make_imitate_plan_tools, ()),
        (make_imitate_scaffold_tools, ()),
        (make_imitate_build_tools, ([],)),
        (make_imitate_verify_tools, ()),
        (make_imitate_final_tools, ()),
    ]
    for factory, extra in factories:
        event = asyncio.Event()
        data: dict[str, object] = {}
        tools = factory(event, data, *extra)
        assert tools
        tool = tools[0]
        kwargs: dict[str, object] = {}
        for parameter in inspect.signature(tool).parameters.values():
            if parameter.default is inspect.Parameter.empty:
                if parameter.name in {"routes", "interactions", "assets", "issues"}:
                    kwargs[parameter.name] = []
                elif parameter.name in {"design_tokens", "component_map"}:
                    kwargs[parameter.name] = {}
                elif parameter.name == "satisfied":
                    kwargs[parameter.name] = False
                else:
                    kwargs[parameter.name] = ""
        result = await tool(**kwargs)
        assert result["ok"] is False, (getattr(tool, "__name__", ""), result)
        assert not event.is_set()

    event = asyncio.Event()
    data: dict[str, object] = {}
    reject_parity = make_copy_parity_tools(event, data)[1]
    result = await reject_parity("layout gap")
    assert result["ok"] is True
    assert event.is_set()


def test_website_state_helpers_and_cache_contracts() -> None:
    assert CopyWebsiteState.COMPLETE.is_terminal
    assert not CopyWebsiteState.EXTRACT_TARGET.is_terminal
    assert SiteImitateState.FAILED.is_terminal
    assert ReconstructState.BLOCKED.is_terminal
    assert "CACHE-STABLE" in COPY_CACHE_CONTRACT
    assert "CACHE-STABLE" in RECONSTRUCT_CACHE_CONTRACT
    assert "MOBILE-FIRST" in MOBILE_RESPONSIVE_CONTRACT

    copy = _runner(CopyWebsiteRunner)
    ctx = CopyContext("intent", "run", CopyWebsiteState.IMPLEMENT_LAYOUT)
    assert copy._fix_context(ctx) == ""
    ctx.fix_reason = "wrong spacing"
    ctx.fix_iterations = 2
    assert "iteration 2" in copy._fix_context(ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runner_cls", "context", "method"),
    [
        (
            _runner(CopyWebsiteRunner),
            CopyContext("i", "r", CopyWebsiteState.EXTRACT_TARGET),
            "_extract_target",
        ),
        (
            _runner(CopyWebsiteRunner),
            CopyContext("i", "r", CopyWebsiteState.SITE_STUDY),
            "_site_study",
        ),
        (
            _runner(CopyWebsiteRunner),
            CopyContext("i", "r", CopyWebsiteState.DESIGN_SPEC),
            "_design_spec",
        ),
        (_runner(CopyWebsiteRunner), CopyContext("i", "r", CopyWebsiteState.SCAFFOLD), "_scaffold"),
        (
            _runner(CopyWebsiteRunner),
            CopyContext("i", "r", CopyWebsiteState.IMPLEMENT_LAYOUT),
            "_implement_layout",
        ),
        (
            _runner(CopyWebsiteRunner),
            CopyContext("i", "r", CopyWebsiteState.IMPLEMENT_PAGES),
            "_implement_pages",
        ),
        (
            _runner(CopyWebsiteRunner),
            CopyContext("i", "r", CopyWebsiteState.IMPLEMENT_DATA),
            "_implement_data",
        ),
        (
            _runner(CopyWebsiteRunner),
            CopyContext("i", "r", CopyWebsiteState.RESPONSIVE_PASS),
            "_responsive_pass",
        ),
        (
            _runner(CopyWebsiteRunner),
            CopyContext("i", "r", CopyWebsiteState.PARITY_VERIFY),
            "_parity_verify",
        ),
    ],
)
async def test_copy_phase_failure_is_bounded(
    runner_cls: Any, context: CopyContext, method: str
) -> None:
    async def no_transition(**_kwargs: object) -> None:
        return None

    runner_cls.run_phase = no_transition  # type: ignore[method-assign]
    result = await getattr(runner_cls, method)(context, object())
    assert result is CopyWebsiteState.FAILED
    assert context.fail_reason


@pytest.mark.asyncio
async def test_reconstruct_validation_rejection_reenters_then_approves() -> None:
    runner = _runner(ReconstructSiteRunner)
    reject_once = {
        "visual_rejected": 0,
        "interaction_rejected": 0,
        "a11y_rejected": 0,
        "perf_rejected": 0,
        "fidelity_rejected": 0,
    }

    async def reject_then_approve(**kwargs: object) -> None:
        tools = kwargs["tools"]
        assert isinstance(tools, list)
        for tool in tools:
            name = getattr(tool, "__name__", "")
            if name in reject_once and reject_once[name] == 0:
                reject_once[name] += 1
                await tool([{"issue": "minor discrepancy"}], "")  # type: ignore[operator]
                return
        for tool in tools:
            name = getattr(tool, "__name__", "")
            if name in {
                "visual_approved",
                "interaction_approved",
                "a11y_approved",
                "perf_approved",
                "fidelity_approved",
            }:
                await tool("verified after the repair")  # type: ignore[operator]
                return

    runner.run_phase = reject_then_approve  # type: ignore[method-assign]
    ctx = _reconstruct_context()
    assert await runner._visual_validation(ctx, object()) is ReconstructState.INTERACTION_VALIDATION
    assert await runner._interaction_validation(ctx, object()) is ReconstructState.ACCESSIBILITY
    assert await runner._accessibility(ctx, object()) is ReconstructState.PERFORMANCE
    assert await runner._performance(ctx, object()) is ReconstructState.FIDELITY_PASS
    assert await runner._fidelity_pass(ctx, object()) is ReconstructState.SQLITE_DB
    assert len(ctx.visual_discrepancies) == 2
    assert len(ctx.interaction_discrepancies) == 1


@pytest.mark.asyncio
async def test_reconstruct_validation_can_reenter_an_earlier_phase() -> None:
    runner = _runner(ReconstructSiteRunner)
    methods = (
        ("_visual_validation", "visual_rejected"),
        ("_interaction_validation", "interaction_rejected"),
        ("_accessibility", "a11y_rejected"),
        ("_performance", "perf_rejected"),
        ("_fidelity_pass", "fidelity_rejected"),
    )
    for method, rejected_name in methods:

        async def reject_to_page(**kwargs: object) -> None:
            tools = kwargs["tools"]
            assert isinstance(tools, list)
            rejected = next(
                tool for tool in tools if getattr(tool, "__name__", "") == rejected_name
            )
            parameters = inspect.signature(rejected).parameters
            if "discrepancies" in parameters:
                await rejected([{"issue": "layout"}], "page")  # type: ignore[operator]
            elif "issues" in parameters:
                await rejected([{"issue": "layout"}], "page")  # type: ignore[operator]
            else:
                raise AssertionError(rejected_name)

        runner.run_phase = reject_to_page  # type: ignore[method-assign]
        ctx = _reconstruct_context()
        assert await getattr(runner, method)(ctx, object()) is ReconstructState.PAGE
        assert ctx.known_issues[-1]["phase"]


@pytest.mark.asyncio
async def test_reconstruct_infrastructure_verification_retries_after_rejection() -> None:
    runner = _runner(ReconstructSiteRunner)
    methods = (
        ("_verify_sqlite", "sqlite_rejected", "sqlite_verified"),
        ("_verify_prisma", "prisma_rejected", "prisma_verified"),
        ("_verify_tanstack", "tanstack_rejected", "tanstack_verified"),
        ("_verify_env", "env_rejected", "env_verified"),
        ("_verify_docker", "docker_rejected", "docker_verified"),
        ("_verify_netlify", "netlify_rejected", "netlify_verified"),
        ("_verify_caddy", "caddy_rejected", "caddy_verified"),
        ("_verify_package", "package_rejected", "package_verified"),
        ("_verify_scripts", "scripts_rejected", "scripts_verified"),
        ("_verify_docs", "docs_rejected", "docs_verified"),
    )
    for method, rejected_name, verified_name in methods:
        rejected_once = False

        async def reject_then_verify(**kwargs: object) -> None:
            nonlocal rejected_once
            tools = kwargs["tools"]
            assert isinstance(tools, list)
            if not rejected_once:
                rejected = next(
                    tool for tool in tools if getattr(tool, "__name__", "") == rejected_name
                )
                await rejected(["one issue"])  # type: ignore[operator]
                rejected_once = True
                return
            verified = next(
                tool for tool in tools if getattr(tool, "__name__", "") == verified_name
            )
            await verified("verified after repair")  # type: ignore[operator]

        runner.run_phase = reject_then_verify  # type: ignore[method-assign]
        ctx = _reconstruct_context()
        result = await getattr(runner, method)(ctx, object())
        assert result.name not in {"FAILED", "BLOCKED"}
        assert ctx.infra_status
        assert any(issue["issue"] == "one issue" for issue in ctx.known_issues)


@pytest.mark.asyncio
async def test_reconstruct_transition_factories_cover_all_input_guards() -> None:
    import agenthicc.workflows.reconstruct_site.runner as module

    factory_names = (
        "_make_init_tools",
        "_make_recon_tools",
        "_make_visual_research_tools",
        "_make_interaction_analysis_tools",
        "_make_content_assets_tools",
        "_make_architecture_tools",
        "_make_design_system_tools",
        "_make_bootstrap_tools",
        "_make_global_shell_tools",
        "_make_component_system_tools",
        "_make_page_tools",
        "_make_data_layer_tools",
        "_make_responsive_pass_tools",
        "_make_visual_validation_tools",
        "_make_interaction_validation_tools",
        "_make_accessibility_tools",
        "_make_performance_tools",
        "_make_fidelity_pass_tools",
        "_make_final_validation_tools",
        "_make_sqlite_tools",
        "_make_verify_sqlite_tools",
        "_make_prisma_tools",
        "_make_verify_prisma_tools",
        "_make_tanstack_tools",
        "_make_verify_tanstack_tools",
        "_make_env_tools",
        "_make_verify_env_tools",
        "_make_docker_tools",
        "_make_verify_docker_tools",
        "_make_netlify_tools",
        "_make_verify_netlify_tools",
        "_make_caddy_tools",
        "_make_verify_caddy_tools",
        "_make_package_tools",
        "_make_verify_package_tools",
        "_make_scripts_tools",
        "_make_verify_scripts_tools",
        "_make_docs_tools",
        "_make_verify_docs_tools",
    )
    for factory_name in factory_names:
        event = asyncio.Event()
        data: dict[str, object] = {}
        factory = getattr(module, factory_name)
        tools = factory(event, data)
        for tool in tools:
            name = getattr(tool, "__name__", "")
            parameters = inspect.signature(tool).parameters
            if "rejected" in name or name in {"final_blocked", "reject"}:
                kwargs = {}
                for parameter in parameters.values():
                    if parameter.default is inspect.Parameter.empty:
                        kwargs[parameter.name] = (
                            ["issue"] if parameter.name in {"issues", "discrepancies"} else "issue"
                        )
                await tool(**kwargs)
                continue
            kwargs = {}
            for parameter in parameters.values():
                if parameter.default is inspect.Parameter.empty:
                    if parameter.name in {
                        "routes",
                        "interactions",
                        "assets",
                        "issues",
                        "discrepancies",
                    }:
                        kwargs[parameter.name] = []
                    elif parameter.name in {"design_tokens", "component_map"}:
                        kwargs[parameter.name] = {}
                    else:
                        kwargs[parameter.name] = ""
            result = await tool(**kwargs)
            if result["ok"]:
                # Verdict tools intentionally accept an empty summary; the
                # runner records the decision and validates the surrounding
                # phase contract.
                assert event.is_set(), (factory_name, name, result)
            else:
                assert not event.is_set(), (factory_name, name, result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method",
    (
        "_init",
        "_recon",
        "_visual_research",
        "_interaction_analysis",
        "_content_assets",
        "_architecture",
        "_design_system",
        "_bootstrap",
        "_global_shell",
        "_component_system",
        "_page",
        "_data_layer",
        "_responsive_pass",
        "_visual_validation",
        "_interaction_validation",
        "_accessibility",
        "_performance",
        "_fidelity_pass",
        "_sqlite_db",
        "_verify_sqlite",
        "_prisma",
        "_verify_prisma",
        "_tanstack_query",
        "_verify_tanstack",
        "_env_config",
        "_verify_env",
        "_docker",
        "_verify_docker",
        "_netlify",
        "_verify_netlify",
        "_caddy",
        "_verify_caddy",
        "_package_commands",
        "_verify_package",
        "_scripts",
        "_verify_scripts",
        "_docs",
        "_verify_docs",
        "_final_validation",
    ),
)
async def test_reconstruct_phase_failure_is_bounded(method: str) -> None:
    runner = _runner(ReconstructSiteRunner)

    async def no_transition(**_kwargs: object) -> None:
        return None

    runner.run_phase = no_transition  # type: ignore[method-assign]
    context = _reconstruct_context()
    assert await getattr(runner, method)(context, object()) is ReconstructState.FAILED
    assert context.fail_reason


@pytest.mark.asyncio
async def test_reconstruct_checkpoint_codec_normalizes_and_rejects_bad_state() -> None:
    from agenthicc.workflows.reconstruct_site.runner import ReconstructSiteWorkflow

    ctx = _reconstruct_context()
    ctx.state = ReconstructState.COMPLETE
    payload = ReconstructSiteWorkflow.checkpoint_context_to_payload(ctx)
    restored = ReconstructSiteWorkflow.checkpoint_context_from_payload(payload, memory="session")
    assert restored.state is ReconstructState.COMPLETE
    assert restored.shared_memory == "session"
    assert restored.pages_to_implement == ["/"]

    payload["state"] = "NOT_A_STATE"
    with pytest.raises(ValueError, match="unknown reconstruct_site state"):
        ReconstructSiteWorkflow.checkpoint_context_from_payload(payload)
