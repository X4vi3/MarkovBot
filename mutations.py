import random

import config

EMOJIS = [
    "💀", "🗿", "😭", "🤡", "👺", "🔥", "💅", "😈",
    "🫠", "🤯", "👁", "🐸", "⚡", "🌚", "😎", "🤨",
]


def _all_caps(text: str) -> str:
    return text.upper()


def _double_word(text: str) -> str:
    words = text.split()
    if len(words) < 2:
        return text
    idx = random.randint(0, len(words) - 1)
    words.insert(idx, words[idx])
    return " ".join(words)


def _add_emoji(text: str) -> str:
    return text + " " + random.choice(EMOJIS)


def _clap(text: str) -> str:
    return text.replace(" ", " 👏 ")


def _mock_case(text: str) -> str:
    return "".join(
        ch.upper() if random.random() > 0.5 else ch.lower() for ch in text
    )


def _stretch(text: str) -> str:
    """Растягивает рандомные гласные: привет -> прииивееет"""
    vowels = "аеёиоуыэюяaeiouy"
    result = []
    for ch in text:
        if ch.lower() in vowels and random.random() < 0.3:
            result.append(ch * random.randint(2, 4))
        else:
            result.append(ch)
    return "".join(result)


def _reverse_words(text: str) -> str:
    """Переворачивает порядок слов."""
    words = text.split()
    if len(words) < 2:
        return text
    words.reverse()
    return " ".join(words)


def _stutter(text: str) -> str:
    """Заикание: привет как дела -> п-привет к-как д-дела"""
    words = text.split()
    result = []
    for word in words:
        if len(word) > 1 and random.random() < 0.4:
            result.append(f"{word[0]}-{word}")
        else:
            result.append(word)
    return " ".join(result)


_MUTATIONS = [
    _all_caps, _double_word, _add_emoji, _clap,
    _mock_case, _stretch, _reverse_words, _stutter,
]

COMBO_CHANCE = 0.3  # шанс применить вторую мутацию поверх первой


def mutate(text: str, chance: float | None = None) -> str:
    """Применяет случайную мутацию к тексту с вероятностью chance.

    Если chance не задан — используется значение config.MUTATION_CHANCE.
    Передавай per-chat значение из панели управления, чтобы настройка
    «Мутация» в /panel реально влияла на бота.
    """
    if chance is None:
        chance = config.MUTATION_CHANCE
    if random.random() > chance:
        return text
    fn = random.choice(_MUTATIONS)
    text = fn(text)
    # комбо-мутация для максимального хаоса
    if random.random() < COMBO_CHANCE:
        fn2 = random.choice(_MUTATIONS)
        text = fn2(text)
    return text
