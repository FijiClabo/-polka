import pytest

from ai.checker import CheckInput, check_retelling
from ai.llm import LLMChain, LLMError, LLMResult, LLMUnavailable, set_llm
from ai.verdict import VerdictParseError, apply_rules, parse_verdict


def test_parse_valid_json():
    v = parse_verdict(
        '{"verdict":"accepted","confidence":0.9,"reply":"Хорошо подмечено!!!","question":null,'
        '"note_for_summary":"Иван пишет заявление."}'
    )
    assert v.verdict == "accepted"
    assert v.reply == "Хорошо подмечено!"  # без восклицательных знаков подряд
    assert v.question is None


def test_parse_json_in_code_fence_and_text():
    v = parse_verdict('Вот ответ:\n```json\n{"verdict":"clarify","confidence":0.4,"reply":"Расскажи подробнее.",'
                      '"question":"Чем закончился сеанс?","note_for_summary":""}\n```')
    assert v.verdict == "clarify" and v.question == "Чем закончился сеанс?"


@pytest.mark.parametrize(
    "raw",
    ["", "не json", '{"verdict":"maybe","reply":"x"}', '{"verdict":"accepted","reply":""}', "[1,2]"],
)
def test_parse_invalid(raw):
    with pytest.raises(VerdictParseError):
        parse_verdict(raw)


def _v(verdict, question=None):
    return parse_verdict(
        f'{{"verdict":"{verdict}","confidence":0.5,"reply":"Ответ.","question":'
        f'{("null" if question is None else chr(34) + question + chr(34))},"note_for_summary":"заметка"}}'
    )


def test_paper_book_never_rejected():
    assert apply_rules(_v("rejected"), has_text=False, clarify_count=0).verdict == "clarify"
    v = apply_rules(_v("rejected"), has_text=False, clarify_count=2)
    assert v.verdict == "accepted" and v.verified is False


def test_max_two_clarify_then_accept():
    assert apply_rules(_v("clarify", "Вопрос?"), has_text=True, clarify_count=1).verdict == "clarify"
    v = apply_rules(_v("clarify", "Вопрос?"), has_text=True, clarify_count=2)
    assert v.verdict == "accepted" and v.question is None


def test_rejected_allowed_with_text():
    v = apply_rules(_v("rejected"), has_text=True, clarify_count=0)
    assert v.verdict == "rejected" and v.note_for_summary == "" and v.verified


def test_clarify_always_has_question():
    v = apply_rules(_v("clarify"), has_text=True, clarify_count=0)
    assert v.question


# --------------------------------------------------------------------------- checker с подменённым ИИ


class FakeProvider:
    def __init__(self, name, answers):
        self.name = name
        self.answers = list(answers)
        self.calls = 0

    async def complete(self, system, context, prompt, *, schema, cheap, max_tokens):
        self.calls += 1
        a = self.answers.pop(0) if self.answers else self.answers_default
        if isinstance(a, Exception):
            raise a
        return LLMResult(a, self.name, "fake")

    answers_default = '{"verdict":"accepted","confidence":1,"reply":"Ок.","question":null,"note_for_summary":"n"}'


INP = CheckInput(
    title="Книга", author="Автор", segment_title="Глава 1", day_number=1, pages="стр. 1–10",
    segment_text="Текст отрезка", retelling="Пересказ " * 10,
)
GOOD = '{"verdict":"accepted","confidence":0.9,"reply":"Засчитано.","question":null,"note_for_summary":"n"}'


async def test_retry_once_on_invalid_json():
    p = FakeProvider("a", ["мусор", GOOD])
    set_llm(LLMChain([p]))
    v = await check_retelling(INP)
    assert v.verdict == "accepted" and not v.fallback and p.calls == 2


async def test_invalid_json_twice_accepts_without_verification():
    p = FakeProvider("a", ["мусор", "снова мусор"])
    set_llm(LLMChain([p]))
    v = await check_retelling(INP)
    assert v.verdict == "accepted" and v.fallback and v.verified is False


async def test_fallback_to_second_provider():
    a = FakeProvider("anthropic", [LLMError("down")])
    b = FakeProvider("yandex", [GOOD])
    set_llm(LLMChain([a, b]))
    v = await check_retelling(INP)
    assert v.provider == "yandex"


async def test_all_providers_down_raises_unavailable():
    a = FakeProvider("anthropic", [LLMError("down")])
    b = FakeProvider("yandex", [LLMError("down")])
    set_llm(LLMChain([a, b]))
    with pytest.raises(LLMUnavailable):
        await check_retelling(INP)
    set_llm(None)
