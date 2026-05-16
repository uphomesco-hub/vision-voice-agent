from server import _is_probable_answer_recitation, _is_question_or_coding_request


def test_coding_question_is_actionable():
    assert _is_question_or_coding_request("How do I fix this React useEffect loop?")
    assert _is_question_or_coding_request("debug the backend websocket error")


def test_answer_recitation_is_detected():
    answer = (
        "Use a debounce around the input handler, keep the timeout id in a ref, "
        "and clear the previous timer before scheduling the next request."
    )
    spoken = "use a debounce around the input handler and keep the timeout id in a ref"

    is_recitation, metrics = _is_probable_answer_recitation(spoken, answer)

    assert is_recitation
    assert metrics["overlap"] > 0.5


def test_followup_question_is_not_recitation():
    answer = (
        "Use a debounce around the input handler, keep the timeout id in a ref, "
        "and clear the previous timer before scheduling the next request."
    )
    spoken = "Can you show me the React code for that debounce?"

    is_recitation, _ = _is_probable_answer_recitation(spoken, answer)

    assert not is_recitation
