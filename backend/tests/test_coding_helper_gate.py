from server import (
    _clean_voice_text,
    _is_probable_answer_recitation,
    _is_question_or_coding_request,
    _is_stt_artifact,
    _merge_voice_fragment,
)


def test_coding_question_is_actionable():
    assert _is_question_or_coding_request("How do I fix this React useEffect loop?")
    assert _is_question_or_coding_request("debug the backend websocket error")
    assert _is_question_or_coding_request("I need to create a new branch for the feature I'm working on.")
    assert _is_question_or_coding_request("Can you tell me more about AWS EC2?")
    assert _is_question_or_coding_request("What is a closure?")
    assert _is_question_or_coding_request("difference between list and tuple")


def test_statement_with_coding_terms_is_not_automatically_actionable():
    assert not _is_question_or_coding_request("You can use the AWS EC2 service to deploy your API backend.")
    assert not _is_question_or_coding_request("Python is a high-level interpreted programming language.")
    assert not _is_question_or_coding_request("What can I help you with today?")


def test_stt_prompt_leak_is_artifact():
    assert _is_stt_artifact(
        "context: ### Coding assistant dictation. Expect terms like Python, JavaScript, React, Swift, Xcode, GitHub, AWS, EC2. ###"
    )
    assert _is_stt_artifact(
        "You will receive additional context/instructions (separated by ### delimiters) from the user."
    )
    assert _is_stt_artifact("Sure, I'm ready to assist with any coding-related queries or tasks you have.")


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
