from walter.tags import TagStream, speakable


def run(chunks):
    ts = TagStream()
    out = []
    for c in chunks:
        out += ts.feed(c)
    return out + ts.flush()


def test_emotion_and_sentences():
    out = run(["[happy] Oh hey, you're back! [action:bounce] How did it go?"])
    assert out == [("emotion", "happy"), ("say", "Oh hey, you're back!"),
                   ("action", "bounce"), ("say", "How did it go?")]


def test_streaming_split_tags_and_sentences():
    text = "[sad] That's a shame. [curi" + "ous] Want to talk about it?"
    chunks = [text[i:i + 3] for i in range(0, len(text), 3)]
    assert run(chunks) == [("emotion", "sad"), ("say", "That's a shame."),
                           ("emotion", "curious"), ("say", "Want to talk about it?")]


def test_sentence_emitted_before_end_of_stream():
    ts = TagStream()
    assert ts.feed("Hello there. And then") == [("say", "Hello there.")]
    assert ts.flush() == [("say", "And then")]


def test_silent_and_unknown_tags():
    assert run(["[silent]"]) == [("silent", None)]
    assert run(["[blah] hi"]) == [("say", "hi")]


def test_code_and_markdown_not_spoken():
    out = run(["Here **it** is:\n```\nrm -rf /\n```\nDone. See https://x.y/z ok"])
    assert out == [("say", "Here it is:"), ("say", "Done."), ("say", "See a link ok")]


def test_numbers_not_split():
    assert run(["It costs 3.50 today."]) == [("say", "It costs 3.50 today.")]


def test_speakable():
    assert speakable("# Title\n- one\n- two") == "Title one two"


# ---- sentence splitting: abbreviations, short bits, random streaming chunks
import random  # noqa: E402

import pytest  # noqa: E402

from walter.tags import speech_text  # noqa: E402

REPLIES = [
    "[calm] It's 9:32 a.m. on Wednesday, Nov. 4th. [action:nod] Emma's piano lesson starts at 10:15.",
    "[happy] Maria's calendar says the game is at 6:30 p.m., so you've got time.",
    "[curious] Want me to check example-owner/example-repo on GitHub? It has two new issues.",
    "It costs $3.50, about 15% more than last year (roughly $0.45 extra).",
    "Three things: milk, eggs and bread. Anything else?",
    "[thinking] Hmm... I'm not sure — maybe Mr. Smith knows? e.g. ask him tomorrow.",
    "[excited] [action:bounce] It's DONE! The build passed in 2 min 30 s.",
    "1. Open the app\n2. Tap Settings\n3. Turn on captions",
    "Version 2.1.3 of the U.S. app is out vs. the old one, etc. So update it.",
    "Ha! [laugh] That's a good one. [wink] J. R. Smith agrees.",
]


def says(events):
    return [v for k, v in events if k == "say"]


@pytest.mark.parametrize("reply", REPLIES)
def test_random_chunking_gives_identical_ordered_events(reply):
    whole = run([reply])
    rng = random.Random(hash(reply) & 0xffff)
    for _ in range(100):
        chunks, i = [], 0
        while i < len(reply):
            n = rng.randint(1, 7)
            chunks.append(reply[i:i + n])
            i += n
        assert run(chunks) == whole
    # nothing lost or reordered: the spoken words are the reply's words, in order
    flat = " ".join(says(whole))
    plain = re.sub(r"\[[^\]]*\]", " ", reply)
    assert re.findall(r"\w+", flat) == re.findall(r"\w+", speakable(plain))


import re  # noqa: E402


def test_abbreviations_dont_end_sentences():
    assert says(run(["It's 9:32 a.m. on Wednesday, Nov. 4th. Next one."])) == [
        "It's 9:32 a.m. on Wednesday, Nov. 4th.", "Next one."]
    assert says(run(["Ask Mr. Smith or Dr. Jones. Then J. R. Smith."])) == [
        "Ask Mr. Smith or Dr. Jones.", "Then J. R. Smith."]
    assert says(run(["Version 2 of the U.S. app is out vs. the old one."])) == [
        "Version 2 of the U.S. app is out vs. the old one."]


def test_soft_abbreviation_waits_for_next_word():
    ts = TagStream()
    assert ts.feed("See you at 9 a.m. ") == []          # can't tell yet
    assert says(ts.feed("Want coffee? ")) == ["See you at 9 a.m.", "Want coffee?"]
    assert says(run(["See you at 9 a.m. tomorrow then."])) == ["See you at 9 a.m. tomorrow then."]
    assert says(run(["See you at 9 a.m."])) == ["See you at 9 a.m."]


def test_tiny_sentences_merge_with_the_next():
    assert says(run(["Hmm... I'm not sure. Okay then."])) == ["Hmm... I'm not sure.", "Okay then."]
    # but a tag right after still fires in order
    assert run(["Ha! [laugh] Good one."]) == [("say", "Ha!"), ("action", "laugh"), ("say", "Good one.")]
    assert run(["Done."]) == [("say", "Done.")]


def test_emoji_never_reach_captions_or_voice():
    assert says(run(["Nice one 🙂 really."])) == ["Nice one really."]


@pytest.mark.parametrize("text,spoken", [
    ("It's 9:32 a.m. on Nov. 4th.", "It's 9 32 AM on November 4th."),
    ("At 10:00 tonight, or 12:05 PM.", "At 10 o'clock tonight, or 12 oh 5 PM."),
    ("It costs $3.50, or $0.45, or $1.", "It costs 3 dollars 50, or 45 cents, or 1 dollar."),
    ("Ask Mr. Smith, e.g. tomorrow.", "Ask Mister Smith, for example tomorrow."),
    ("Hmm... I'm not sure — maybe.", "Hmm, I'm not sure, maybe."),
    ("Winds of 10-15 mph and 45°F.", "Winds of 10 to 15 mph and 45 degrees Fahrenheit."),
    ("Restarting now...", "Restarting now."),
    ("Call 212-555-1234", "Call 212-555-1234"),
    ("The answer is no. Sorry.", "The answer is no. Sorry."),
])
def test_speech_text(text, spoken):
    assert speech_text(text) == spoken


def test_first_clause_of_a_long_first_sentence_is_spoken_early():
    ts = TagStream()
    assert ts.feed("[happy] Honestly, I'd skip that one, the reviews") == [("emotion", "happy")]
    assert ts.feed(" are ") == [("say", "Honestly, I'd skip that one,")]
    assert ts.feed("mixed. And the second sentence, which is long, stays whole.") == [
        ("say", "the reviews are mixed.")]
    assert says(ts.flush()) == ["And the second sentence, which is long, stays whole."]


def test_short_clauses_and_stubs_are_not_split():
    assert says(run(["Oh, nice, he held on."])) == ["Oh, nice, he held on."]
    assert says(run(["It's 9:32 a.m. on Wednesday, Nov. 4th."])) == ["It's 9:32 a.m. on Wednesday, Nov. 4th."]


def test_clause_split_waits_for_enough_of_the_rest():
    ts = TagStream()
    assert ts.feed("It's sunny and warm in town right now; perf") == []
    assert ts.feed("ect for a walk") == [("say", "It's sunny and warm in town right now,")]
    assert says(ts.flush()) == ["perfect for a walk"]


def test_clause_split_rearmed_after_a_tool_call():
    ts = TagStream()
    assert says(ts.feed("One sec, let me look. ")) == ["One sec, let me look."]
    ts.arm_clause()
    out = ts.feed("It looks like rain tomorrow afternoon, so mow the lawn in the morning. ")
    assert says(out) == ["It looks like rain tomorrow afternoon,", "so mow the lawn in the morning."]
