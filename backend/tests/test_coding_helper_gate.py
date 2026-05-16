from server import _clean_voice_text, _is_probable_answer_recitation, _is_question_or_coding_request, _merge_voice_fragment


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


def test_voice_stutter_cleanup_repairs_definition_question():
    assert _clean_voice_text("What what it is by this Python?") == "What is Python?"
    assert _clean_voice_text("Here here is a coding coding question question for you.") == "Here is a coding question for you."


def test_streaming_voice_fragments_merge_overlap():
    text = _merge_voice_fragment("What is", "is Python?")
    assert text == "What is Python?"

    cumulative = _merge_voice_fragment("What is", "What is Python?")
    assert cumulative == "What is Python?"
