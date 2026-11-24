"""How a new thing you say relates to the request Jay is still working on.

People pause mid-thought, add a second part ("...and then open the folder"), or correct themselves
("no, I meant ..."). Jay should neither throw the first part away nor ignore the correction, so a
new input arriving while a request is in progress is classified as:

    "continue"  an addition to the same request (merge the two and handle them together)
    "correct"   a correction or replacement (drop the old request, keep it as context)
    "new"       something unrelated

Only wording and timing are used (no extra model call), so it costs nothing and cannot lag.
"""
from __future__ import annotations

import re

FILLERS = {"uh", "um", "umm", "uhh", "hmm", "so", "okay", "ok", "well", "yeah", "hey", "jay"}
CONTINUE = ("and then", "and", "also", "then", "plus", "after that", "next", "additionally",
            "one more thing", "oh and", "while you're at it", "and also", "as well")
CORRECT = ("no", "nope", "wait", "sorry", "actually", "i mean", "i meant", "not that", "instead",
           "scratch that", "cancel that", "change that", "that's wrong", "that is wrong", "wrong",
           "hold on", "not what i", "i said", "i didn't say", "i did not say", "no no")


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", text.lower())


def _lead(text: str) -> list[str]:
    words = _words(text)
    while words and words[0] in FILLERS:
        words = words[1:]
    return words


def _starts_with(words: list[str], phrases: tuple[str, ...]) -> bool:
    for phrase in phrases:
        head = phrase.split()
        if words[:len(head)] == head:
            return True
    return False


def classify(text: str, gap: float | None, window: float) -> str:
    """gap: seconds since the previous input (None for typed text, where only wording counts);
    window: how close in time a spoken addition has to be to count as a continuation."""
    words = _lead(text)
    if _starts_with(words, CORRECT):
        return "correct"
    if _starts_with(words, CONTINUE):
        return "continue"
    if gap is not None and gap <= window:
        return "continue"
    return "new"


def merge(previous: str, addition: str) -> str:
    """Join two spoken parts into one request, dropping a leading 'and' that only linked them."""
    previous = previous.strip()
    addition = addition.strip()
    if not previous:
        return addition
    if previous[-1] not in ".?!":
        previous += "."
    return f"{previous} {addition}"


def interrupted_note(previous: str, steps_done: int) -> str:
    """Context for the model when a correction replaces a request that was already under way."""
    done = f" It had already carried out {steps_done} step(s) of it." if steps_done else ""
    return f'The user interrupted this earlier request before it finished: "{previous.strip()[:240]}".{done}'
