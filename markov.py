import math
import random
import logging
import time

import markovify

try:
    import pymorphy3
    _morph = pymorphy3.MorphAnalyzer()
except ImportError:
    _morph = None

import config
from config import MARKOV_STATE

logger = logging.getLogger(__name__)

AUTO_STATE2_THRESHOLD = 200
SEED_CHANCE = 0.3
REBUILD_EVERY = 500  # пересобирать модель каждые N новых сообщений (при 100к+ реже)

_cached_model: markovify.Text | None = None
_cached_count: int = 0


def get_model(current_count: int, messages_fn) -> markovify.Text | None:
    """Возвращает кэшированную модель, пересобирая раз в REBUILD_EVERY сообщений."""
    global _cached_model, _cached_count

    if _cached_model is not None and current_count - _cached_count < REBUILD_EVERY:
        return _cached_model

    messages = messages_fn()
    logger.info(f"Пересборка модели: {len(messages)} сообщений (было {_cached_count}, стало {current_count})")
    _cached_model = build_model(messages)
    _cached_count = current_count
    return _cached_model


def invalidate_cache():
    """Сбросить кэш (например после /reset)."""
    global _cached_model, _cached_count, _idf_cache, _idf_corpus_size
    global _filtered_corpus, _filtered_corpus_size, _lemma_cache
    global _word_cache, _word_cache_size
    _cached_model = None
    _cached_count = 0
    _idf_cache = {}
    _idf_corpus_size = 0
    _filtered_corpus = []
    _filtered_corpus_size = 0
    _lemma_cache = {}
    _word_cache = set()
    _word_cache_size = 0


def warmup_caches(messages: list[str]) -> None:
    """Прогревает все кэши после построения модели.

    Без прогрева первое контекстное сообщение пользователя упирается в
    холодный pymorphy3 + полный скан корпуса (20–60 секунд). После
    прогрева все последующие запросы работают за миллисекунды.
    """
    if not messages:
        return
    t0 = time.monotonic()
    # 1. Отфильтрованный корпус
    good = _get_filtered_corpus(messages)
    # 2. IDF
    _build_idf(messages)
    # 3. Леммы каждого сообщения (забивает _lemma_cache на все слова корпуса)
    for m in good:
        _extract_lemmas(m)
    # 4. Множество слов корпуса (для команды `v g w`)
    _get_word_set(messages)
    dt = time.monotonic() - t0
    logger.info(
        f"Прогрев кэшей: {len(good)} сообщений, "
        f"{len(_lemma_cache)} лемм, "
        f"{len(_word_cache)} уникальных слов за {dt:.1f}с"
    )


def build_model(messages: list[str]) -> markovify.Text | None:
    corpus = "\n".join(messages)
    state = MARKOV_STATE
    if state == 1 and len(messages) >= AUTO_STATE2_THRESHOLD:
        state = 2

    model = None
    try:
        model = markovify.Text(corpus, state_size=state, well_formed=False)
    except Exception:
        if state > 1:
            try:
                model = markovify.Text(corpus, state_size=1, well_formed=False)
            except Exception:
                return None
        else:
            return None

    # Прогрев: markovify лениво компилирует внутренние структуры цепи
    # при первом вызове make_sentence(). Без прогрева первый
    # make_short_sentence() занимает 30-60 секунд. Делаем сейчас.
    if model is not None:
        try:
            model.make_sentence(tries=10)
        except Exception:
            pass
    return model


def _generate_plain(model: markovify.Text) -> str | None:
    # Меньше tries и проходов: для нашего корпуса 25×2 хватает,
    # а 100×3 могло висеть по нескольку секунд.
    for max_chars in (140, 200):
        for _ in range(2):
            result = model.make_short_sentence(max_chars=max_chars, tries=25)
            if result is not None and not _is_garbage(result):
                return result
    return None


def _generate_seeded(model: markovify.Text, seed_words: list[str]) -> str | None:
    random.shuffle(seed_words)
    # Пробуем не больше двух слов, иначе на нерепрезентативном корпусе
    # tries у make_sentence_with_start умножается на длину списка.
    for word in seed_words[:2]:
        try:
            result = model.make_sentence_with_start(word, strict=False, tries=10)
            if result and len(result) <= 200 and not _is_garbage(result):
                return result
        except (KeyError, markovify.text.ParamError, Exception):
            continue
    return None


MIN_CONTEXT_WORD_LEN = 4  # минимальная длина слова для контекстного поиска

# Слова, которые слишком частые и не дают хорошего контекстного совпадения
_STOP_WORDS = frozenset({
    "что", "это", "как", "так", "все", "всё", "они", "она", "его",
    "для", "тут", "там", "ещё", "еще", "уже", "или", "тоже", "если",
    "когда", "потом", "просто", "будет", "может", "можно", "надо",
    "только", "очень", "было", "были", "была", "этот", "этой", "этом",
    "чтоб", "через", "после", "перед", "какой", "какая", "какие",
    "which", "that", "this", "with", "from", "have", "they", "what",
    "been", "will", "would", "could", "should", "about", "their",
})

# Кэш TF-IDF весов — пересчитывается редко (тяжёлая операция на 100к+ сообщений)
_idf_cache: dict[str, float] = {}
_idf_corpus_size: int = 0
_IDF_SAMPLE = 5000  # считаем IDF по выборке, не по всему корпусу

# Кэш лемматизации (слово → лемма), чтобы не дёргать pymorphy на одни и те же слова
_lemma_cache: dict[str, str] = {}

# Предварительно отфильтрованный корпус для копий
_filtered_corpus: list[str] = []
_filtered_corpus_size: int = 0

# Максимум сообщений для сканирования контекста
_CONTEXT_SCAN_LIMIT = 3000


def _lemma(word: str) -> str:
    """Приводит слово к нормальной форме (лемме). С кэшем."""
    clean = word.lower().rstrip(".,!?:;)»\"'").lstrip("(«\"'")
    if clean in _lemma_cache:
        return _lemma_cache[clean]
    if _morph and clean.isalpha() and len(clean) <= 30:
        result = _morph.parse(clean)[0].normal_form
    else:
        result = clean
    _lemma_cache[clean] = result
    return result


def _extract_lemmas(text: str) -> set[str]:
    """Извлекает множество значимых лемм из текста."""
    return {
        _lemma(w) for w in text.split()
        if len(w) >= MIN_CONTEXT_WORD_LEN and w.lower() not in _STOP_WORDS
    }


def _build_idf(messages: list[str]) -> dict[str, float]:
    """Считает IDF-вес по выборке из корпуса (не по всему — слишком долго)."""
    global _idf_cache, _idf_corpus_size
    # Пересчитываем только если корпус сильно изменился (>10%)
    if _idf_cache and abs(len(messages) - _idf_corpus_size) < _idf_corpus_size * 0.1:
        return _idf_cache

    # Берём выборку для подсчёта частот
    sample = messages if len(messages) <= _IDF_SAMPLE else random.sample(messages, _IDF_SAMPLE)

    doc_freq: dict[str, int] = {}
    for m in sample:
        lemmas = _extract_lemmas(m)
        for lem in lemmas:
            doc_freq[lem] = doc_freq.get(lem, 0) + 1

    n = len(sample)
    _idf_cache = {
        lem: math.log(n / (1 + freq))
        for lem, freq in doc_freq.items()
    }
    _idf_corpus_size = len(messages)
    logger.info(f"IDF пересчитан: {len(_idf_cache)} лемм (выборка {n} из {len(messages)})")
    return _idf_cache


def _get_filtered_corpus(messages: list[str]) -> list[str]:
    """Возвращает закэшированный отфильтрованный корпус для копий."""
    global _filtered_corpus, _filtered_corpus_size
    if _filtered_corpus and abs(len(messages) - _filtered_corpus_size) < 100:
        return _filtered_corpus

    _filtered_corpus = [m for m in messages
                        if 1 <= len(m.split()) <= _MAX_COPY_WORDS
                        and not m.lower().startswith("/")
                        and not _is_garbage(m)]
    _filtered_corpus_size = len(messages)
    logger.info(f"Корпус отфильтрован: {len(_filtered_corpus)} из {len(messages)}")
    return _filtered_corpus


import re

_CMD_FRAGMENTS = {"v g", "v c", "v g d", "v g p", "v g l", "v g w"}
# Паттерн пересланных сообщений: имя, [дата время] или юникод-мусор
_FORWARD_RE = re.compile(r"\[\d{2}\.\d{2}\.\d{4}\s+\d{1,2}:\d{2}\]")
_MAX_COPY_WORDS = 20  # максимум слов для копии из корпуса


def _is_garbage(text: str) -> bool:
    """Проверяет, является ли текст мусором."""
    words = text.split()
    if len(words) < 2:
        return False
    lower = text.lower()

    # Команды бота в любом месте текста
    for cmd in _CMD_FRAGMENTS:
        if cmd in lower:
            return True

    # Пересланные сообщения с датами
    if _FORWARD_RE.search(text):
        return True

    from collections import Counter
    counts = Counter(w.lower() for w in words)
    most_common_word, most_common_count = counts.most_common(1)[0]

    # Повтор слова: мусор если занимает значимую долю
    # "верю верю" (2 слова) — ок, "количество количество вай-фай" — мусор
    if most_common_count >= 2 and len(most_common_word) >= 3 and len(words) > 3:
        return True

    # Повтор коротких слов: "а а а а" (4+ раза)
    if most_common_count >= 4:
        return True

    # Слишком длинное — склеенные сообщения
    if len(words) >= 18:
        return True

    # Слипшиеся слова (повторение одной подстроки без пробелов)
    for w in words:
        if len(w) > 20:
            return True

    # Подчёркивания / бэкслэши — markdown-мусор из экспорта
    if text.count("_") > 3 or text.count("\\") > 1:
        return True

    # Нелатинские/нерусские символы (японские и т.д.)
    for char in text:
        if ord(char) > 0x3000:  # CJK, японские и т.д.
            return True

    # РаНдОмНыЙ РеГиСтР — 30-70% заглавных букв
    alpha_chars = [c for c in text if c.isalpha()]
    if len(alpha_chars) > 10:
        upper_ratio = sum(1 for c in alpha_chars if c.isupper()) / len(alpha_chars)
        if 0.3 < upper_ratio < 0.7:
            return True

    return False


def _generate_copy(messages: list[str], context: str = "",
                    recent_texts: list[str] | None = None) -> str | None:
    """Режим «копия» — сообщение из корпуса.

    Если есть context — ищет по лемматизированным словам с TF-IDF весами.
    Сканирует выборку, не весь корпус (быстро на 100к+).
    """
    # Используем закэшированный отфильтрованный корпус
    good = _get_filtered_corpus(messages)
    if not good:
        return None

    # Антиэхо
    recent_lower = []
    if recent_texts:
        recent_lower = [m.lower().strip() for m in recent_texts if m]

    def _is_echo(msg: str) -> bool:
        ml = msg.lower().strip()
        for r in recent_lower:
            if ml == r:
                return True
            if len(r) > 5 and (r in ml or ml in r):
                return True
        return False

    # Контекстный поиск с морфологией + TF-IDF
    if context:
        ctx_lemmas = _extract_lemmas(context)
        if ctx_lemmas:
            idf = _build_idf(messages)

            # Сканируем выборку, не весь корпус
            pool = good if len(good) <= _CONTEXT_SCAN_LIMIT else random.sample(good, _CONTEXT_SCAN_LIMIT)

            scored = []
            for m in pool:
                if _is_echo(m):
                    continue
                # Быстрая проверка: есть ли хотя бы одно общее слово (без морфологии)
                m_words_lower = {w.lower() for w in m.split() if len(w) >= MIN_CONTEXT_WORD_LEN}
                ctx_words_lower = {w.lower() for w in context.split() if len(w) >= MIN_CONTEXT_WORD_LEN}
                if not (m_words_lower & ctx_words_lower):
                    # Нет даже поверхностного совпадения — пропускаем (экономим pymorphy)
                    continue
                m_lemmas = _extract_lemmas(m)
                overlap = ctx_lemmas & m_lemmas
                if overlap:
                    score = sum(idf.get(lem, 1.0) for lem in overlap)
                    scored.append((score, m))

            if scored:
                scored.sort(key=lambda x: x[0], reverse=True)
                top = scored[:max(3, len(scored) // 5)]
                pick = random.choice(top)[1]
                logger.debug(
                    f"Контекстная копия: '{pick[:50]}' "
                    f"(леммы: {ctx_lemmas}, score={scored[0][0]:.1f}, "
                    f"найдено={len(scored)})"
                )
                return pick

    # Рандомная копия (без эха)
    for _ in range(20):
        pick = random.choice(good)
        if not _is_echo(pick):
            return pick
    return random.choice(good)


MAX_SPLICE_WORDS = 20  # лимит длины склейки


_SPLICE_DEADLINE = 2.0   # секунд — общий таймаут на одну попытку splice
_SPLICE_TRIES = 5        # попыток внутри markovify на каждое стартовое слово
_SPLICE_CANDIDATES = 3   # сколько стартовых сообщений пробовать


def _generate_splice(model: markovify.Text, messages: list[str]) -> str | None:
    """Режим «склейка» — берёт начало реального сообщения, продолжает через Марков.

    Марков подбирает продолжение по цепям, поэтому результат
    плавно перетекает из одного реального сообщения в другое.
    Имеет жёсткий таймаут — если за _SPLICE_DEADLINE секунд ничего
    не получилось, возвращает None (генерация переходит к plain-markov).
    """
    if not messages or len(messages) < 5:
        return None
    good = [m for m in messages
            if 3 <= len(m.split()) <= 15
            and not m.lower().startswith("/")
            and not _is_garbage(m)]
    if not good:
        return None

    deadline = time.monotonic() + _SPLICE_DEADLINE
    random.shuffle(good)
    for msg in good[:_SPLICE_CANDIDATES]:
        if time.monotonic() > deadline:
            return None
        words = msg.split()
        cut = random.randint(1, min(3, len(words) - 1))
        start_word = words[cut - 1]
        try:
            continuation = model.make_sentence_with_start(
                start_word, strict=False, tries=_SPLICE_TRIES
            )
            if not continuation:
                continue
            prefix = " ".join(words[:cut])
            cont_words = continuation.split()
            if cont_words and cont_words[0].lower() == start_word.lower():
                cont_words = cont_words[1:]
            if not cont_words:
                continue
            result_words = prefix.split() + cont_words
            if len(result_words) > MAX_SPLICE_WORDS:
                result_words = result_words[:MAX_SPLICE_WORDS]
            result = " ".join(result_words)
            if _is_garbage(result):
                continue
            return result
        except Exception:
            continue
    return None


GENERATE_DEADLINE = 1.5  # секунд — общий жёсткий таймаут на всю функцию generate


def _emergency_copy(messages):
    """Гарантированно быстрый фолбэк — случайная фраза из корпуса."""
    good = _get_filtered_corpus(messages)
    if good:
        return random.choice(good)
    if messages:
        return random.choice(messages)
    return None


def generate(model: markovify.Text, context: str = "",
             messages: list[str] | None = None,
             sanity: float | None = None,
             recent_texts: list[str] | None = None) -> str | None:
    """Генерация ответа в зависимости от параметра «разумность».

    SANITY = 1.0 → всегда копирует реальное сообщение (умно, но без креатива).
    SANITY = 0.7 → 70% копий по контексту, 30% марковский бред.
    SANITY = 0.3 → 30% копий, 70% марковский бред.
    SANITY = 0.0 → всегда чистый Марков (бредогенератор).

    На каждом шаге проверяем общий дедлайн GENERATE_DEADLINE: если
    превышен, валимся в _emergency_copy и возвращаем хоть что-то.
    """
    if sanity is None:
        sanity = getattr(config, "SANITY", 0.7)
    deadline = time.monotonic() + GENERATE_DEADLINE
    roll = random.random()

    # 1. Высокая разумность: контекстная копия (поиск по смыслу)
    if roll < sanity * 0.7 and messages:
        result = _generate_copy(messages, context=context, recent_texts=recent_texts)
        if result is not None:
            return result
    if time.monotonic() > deadline:
        return _emergency_copy(messages)

    # 2. Средняя разумность: случайная копия из корпуса
    if roll < sanity and messages:
        result = _generate_copy(messages, recent_texts=recent_texts)
        if result is not None:
            return result
    if time.monotonic() > deadline:
        return _emergency_copy(messages)

    # 3. Низкая разумность: марковский бред с затравкой из контекста
    if context and random.random() < SEED_CHANCE:
        seeds = [w for w in context.split() if len(w) > 3]
        if seeds:
            result = _generate_seeded(model, seeds)
            if result is not None:
                return result
    if time.monotonic() > deadline:
        return _emergency_copy(messages)

    # 4. Финальный фолбэк: чистый Марков
    result = _generate_plain(model)
    if result is not None:
        return result

    return _emergency_copy(messages)


def generate_with_start(model: markovify.Text, start: str, messages: list[str] | None = None) -> str | None:
    """Генерация текста с заданным началом.

    Стратегии (выбирается случайно для разнообразия):
    1. Копия из корпуса — реальное сообщение, содержащее start
    2. Склейка — начало из корпуса + продолжение Марковым
    3. Марков seeded — цепь от начального слова
    4. Марков plain + начало — приклеиваем начало к случайной генерации
    """
    words = start.strip().split()
    if not words:
        return _generate_plain(model)

    start_lower = start.lower()

    # Собираем кандидатов из корпуса (с лимитом сканирования)
    corpus_matches: list[str] = []
    if messages:
        scan = messages if len(messages) <= _CONTEXT_SCAN_LIMIT else random.sample(messages, _CONTEXT_SCAN_LIMIT)
        for msg in scan:
            msg_lower = msg.lower()
            if msg_lower.startswith(("v g", "v c", "/")):
                continue
            if start_lower in msg_lower and not _is_garbage(msg):
                idx = msg_lower.find(start_lower)
                tail = msg[idx:].strip()
                if len(tail.split()) > len(words) and len(tail.split()) <= _MAX_COPY_WORDS:
                    corpus_matches.append(tail)

    # Рандомизируем стратегию для разнообразия
    strategies = []
    if corpus_matches:
        strategies.append("copy")
        strategies.append("splice")
    strategies.append("markov_seed")
    strategies.append("markov_plain")
    random.shuffle(strategies)

    for strategy in strategies:
        if strategy == "copy" and corpus_matches:
            return random.choice(corpus_matches)

        elif strategy == "splice" and corpus_matches:
            # Склейка: берём начало из корпуса, продолжаем Марковым
            random.shuffle(corpus_matches)
            for match in corpus_matches[:5]:
                match_words = match.split()
                if len(match_words) < 2:
                    continue
                cut = random.randint(1, min(3, len(match_words) - 1))
                pivot = match_words[cut - 1]
                try:
                    continuation = model.make_sentence_with_start(
                        pivot, strict=False, tries=30
                    )
                    if continuation:
                        prefix = " ".join(match_words[:cut])
                        cont_words = continuation.split()
                        if cont_words and cont_words[0].lower() == pivot.lower():
                            cont_words = cont_words[1:]
                        if cont_words:
                            result = prefix + " " + " ".join(cont_words)
                            result_words = result.split()
                            if len(result_words) > MAX_SPLICE_WORDS:
                                result = " ".join(result_words[:MAX_SPLICE_WORDS])
                            if not _is_garbage(result):
                                return result
                except Exception:
                    continue

        elif strategy == "markov_seed":
            seed_words = list(words)
            random.shuffle(seed_words)
            for word in seed_words:
                for _ in range(3):
                    try:
                        result = model.make_sentence_with_start(
                            word, strict=False, tries=50
                        )
                        if result and not _is_garbage(result):
                            if not result.lower().startswith(start_lower):
                                result = start + " " + result
                            return result
                    except Exception:
                        break

        elif strategy == "markov_plain":
            plain = _generate_plain(model)
            if plain:
                return start + " " + plain

    return None


def generate_long(model: markovify.Text, sentences: int = 5) -> str | None:
    """Генерация длинного текста из нескольких предложений."""
    parts = []
    for _ in range(sentences * 3):  # больше попыток
        s = model.make_sentence(tries=100)
        if s and s not in parts:
            parts.append(s)
        if len(parts) >= sentences:
            break
    return " ".join(parts) if parts else None


def generate_fixed_length(model: markovify.Text, word_count: int) -> str | None:
    """Генерация текста с примерным количеством слов."""
    parts = []
    total_words = 0
    for _ in range(word_count * 2):
        s = model.make_sentence(tries=50)
        if s:
            parts.append(s)
            total_words += len(s.split())
            if total_words >= word_count:
                break
    if not parts:
        return None
    text = " ".join(parts)
    # Обрезаем до нужного количества слов
    words = text.split()
    return " ".join(words[:word_count])


def _is_normal_word(word: str) -> bool:
    """Фильтр мусорных слов: повтор букв, повтор подстрок, спам."""
    if len(word) < 2:
        return False
    from collections import Counter
    counts = Counter(word)
    most_common_count = counts.most_common(1)[0][1]
    # Больше 60% одной буквы — мусор (алёёёёёёё, ааааа)
    if most_common_count / len(word) > 0.6 and len(word) > 4:
        return False
    # Уникальных букв слишком мало для длинного слова
    if len(word) > 8 and len(counts) < 3:
        return False
    # Повтор подстроки (например, "абвабвабв")
    if len(word) > 6:
        for sub_len in range(2, len(word) // 2 + 1):
            sub = word[:sub_len]
            if sub * (len(word) // sub_len) == word[:sub_len * (len(word) // sub_len)]:
                repeats = len(word) // sub_len
                if repeats >= 2 and sub_len * repeats >= len(word) - sub_len:
                    return False
    return True


# Кэш множества «нормальных» слов из корпуса — пересчитываем
# не чаще чем раз на сильное изменение размера корпуса.
_word_cache: set[str] = set()
_word_cache_size: int = 0


def _get_word_set(messages: list[str]) -> set[str]:
    global _word_cache, _word_cache_size
    if _word_cache and abs(len(messages) - _word_cache_size) < 100:
        return _word_cache
    words: set[str] = set()
    for msg in messages:
        for w in msg.split():
            clean = w.strip(".,!?;:()\"'«»—-").lower()
            if clean.isalpha() and _is_normal_word(clean):
                words.add(clean)
    _word_cache = words
    _word_cache_size = len(messages)
    return _word_cache


def generate_word(messages: list[str], length: int | None = None) -> str | None:
    """Генерация случайного слова из корпуса."""
    all_words = _get_word_set(messages)
    if not all_words:
        return None
    if length is not None:
        filtered = [w for w in all_words if len(w) == length]
        if filtered:
            return random.choice(filtered)
        # Если точного нет — ближайшее
        by_dist = sorted(all_words, key=lambda w: abs(len(w) - length))
        return by_dist[0] if by_dist else None
    return random.choice(list(all_words))
