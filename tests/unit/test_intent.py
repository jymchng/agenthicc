"""Unit tests for the intent layer (PRD-02)."""
from __future__ import annotations
import time
import pytest
from agenthicc.kernel import AppState, Intent, IntentStatus, SecurityPolicy, SystemSettings
from agenthicc.workflows.intent import (
    IntentParser, IntentValidator, LlmPlanner, ParsedIntent, StaticPlanner, parse_task_json,
)
pytestmark = pytest.mark.unit

def _base(max_concurrent=5):
    return AppState.create(settings=SystemSettings(max_concurrent_intents=max_concurrent), policy=SecurityPolicy())

def _parsed(goal="refactor auth"):
    return ParsedIntent(goal=goal, raw_text=goal)

def _active(state, iid):
    return state.with_intent(Intent(intent_id=iid, raw_text="x", status=IntentStatus.running, workflow_id=None, created_at=time.time()))

class TestIntentParser:
    def p(self, t): return IntentParser().parse(t)
    def test_strips_please(self): assert not self.p("please refactor auth").goal.lower().startswith("please")
    def test_strips_can_you(self): assert "add" in self.p("can you add login").goal
    def test_strips_i_want_to(self): assert "update" in self.p("i want to update docs").goal
    def test_extracts_files(self): assert "files" in self.p("fix src/auth.py").entities
    def test_extracts_quoted(self): assert "argon2" in self.p('add "argon2" now').entities.get("quoted", [])
    def test_deadline(self): assert "deadline" in self.p("fix within 2 hours").constraints
    def test_high_confidence_action_verb(self): assert self.p("refactor the module").confidence >= 0.8
    def test_low_confidence_no_verb(self): assert self.p("the thing needs work").confidence < 0.8
    def test_empty(self): r = self.p(""); assert r.goal == "" and r.confidence == 0.0
    def test_raw_text_preserved(self): assert self.p("please fix it").raw_text == "please fix it"

class TestIntentValidator:
    def test_ok_below_capacity(self): assert IntentValidator().validate(_parsed(), _base(5)).ok
    def test_fails_at_capacity(self):
        s = _active(_active(_base(2), "i1"), "i2")
        r = IntentValidator().validate(_parsed(), s)
        assert not r.ok and "capacity" in r.reason.lower()
    def test_fails_empty_goal(self): assert not IntentValidator().validate(_parsed(""), _base()).ok
    def test_ok_just_below(self):
        s = _active(_active(_base(3), "i1"), "i2")
        assert IntentValidator().validate(_parsed(), s).ok

class TestParseTaskJson:
    def test_string_array(self): specs = parse_task_json('["A","B"]'); assert len(specs) == 2
    def test_dict_with_deps(self):
        specs = parse_task_json('[{"id":"n1","label":"T","dependencies":["n0"]},{"id":"n0","label":"Z","dependencies":[]}]')
        n1 = next(s for s in specs if s.node_id == "n1"); assert "n0" in n1.dependencies
    def test_embedded_in_prose(self): assert len(parse_task_json('plan: [{"id":"a","label":"A","deps":[]}]')) == 1
    def test_non_json_empty(self): assert parse_task_json("just text") == []

class TestStaticPlanner:
    async def test_json_parsed(self):
        specs = await StaticPlanner().plan(ParsedIntent(goal="", raw_text='[{"id":"n1","label":"Do it","deps":[]}]'))
        assert specs[0].label == "Do it"
    async def test_fallback_plain(self):
        specs = await StaticPlanner().plan(ParsedIntent(goal="refactor auth", raw_text="refactor auth"))
        assert len(specs) == 1 and "refactor auth" in specs[0].label
    async def test_deps_tuple(self):
        specs = await StaticPlanner().plan(ParsedIntent(goal="", raw_text='[{"id":"b","label":"B","deps":["a"]}]'))
        b = next(s for s in specs if s.node_id == "b"); assert isinstance(b.dependencies, tuple)

class _FR:
    def __init__(self, content, raises=False): self._c = content; self._r = raises
    async def run(self, agent, prompt):
        if self._r: raise RuntimeError("err")
        class _R: pass
        _R.content = self._c; return _R()

class TestLlmPlanner:
    async def test_parses_json(self):
        specs = await LlmPlanner(runner=_FR('[{"id":"x","label":"X","deps":[]}]'), agent=object()).plan(ParsedIntent(goal="X", raw_text="X"))
        assert specs[0].node_id == "x"
    async def test_fallback_on_error(self):
        specs = await LlmPlanner(runner=_FR("", raises=True), agent=object()).plan(ParsedIntent(goal="Y", raw_text="Y"))
        assert len(specs) == 1 and "Y" in specs[0].label
    async def test_fallback_unparseable(self):
        specs = await LlmPlanner(runner=_FR("no json here"), agent=object()).plan(ParsedIntent(goal="Z", raw_text="Z"))
        assert len(specs) == 1
