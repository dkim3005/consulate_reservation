"""Korean (Hangul) -> Latin romanization for display.

Hybrid style:
  * Surname: conventional spelling table (Kim, Lee, Park, ...) — what passports use.
  * Given name: Revised Romanization phonetic mapping.

Names already in Latin script pass through unchanged.
"""
from __future__ import annotations

HANGUL_START = 0xAC00
HANGUL_END = 0xD7A3

INITIALS = [
    "g", "kk", "n", "d", "tt", "r", "m", "b", "pp",
    "s", "ss", "", "j", "jj", "ch", "k", "t", "p", "h",
]
MEDIALS = [
    "a", "ae", "ya", "yae", "eo", "e", "yeo", "ye",
    "o", "wa", "wae", "oe", "yo", "u", "wo", "we",
    "wi", "yu", "eu", "ui", "i",
]
FINALS = [
    "", "k", "k", "ks", "n", "nj", "nh", "t", "l",
    "lk", "lm", "lp", "ls", "lt", "lp", "lh", "m",
    "p", "ps", "t", "t", "ng", "t", "t", "k", "t", "p", "h",
]

SURNAMES_1CHAR = {
    "김": "Kim", "이": "Lee", "박": "Park", "최": "Choi", "정": "Jung",
    "강": "Kang", "조": "Cho", "윤": "Yoon", "장": "Jang", "임": "Lim",
    "한": "Han", "신": "Shin", "오": "Oh", "서": "Seo", "권": "Kwon",
    "황": "Hwang", "안": "Ahn", "송": "Song", "류": "Ryu", "유": "Yoo",
    "홍": "Hong", "전": "Jeon", "고": "Ko", "문": "Moon", "양": "Yang",
    "손": "Son", "배": "Bae", "백": "Baek", "허": "Heo", "남": "Nam",
    "심": "Shim", "노": "Noh", "하": "Ha", "곽": "Kwak", "성": "Sung",
    "차": "Cha", "주": "Joo", "우": "Woo", "구": "Koo", "민": "Min",
    "진": "Jin", "지": "Ji", "엄": "Um", "채": "Chae", "원": "Won",
    "천": "Cheon", "방": "Bang", "공": "Gong", "현": "Hyun", "함": "Ham",
    "변": "Byun", "염": "Yeom", "여": "Yeo", "추": "Choo", "도": "Do",
    "소": "So", "석": "Seok", "선": "Sun", "설": "Seol", "마": "Ma",
    "길": "Gil", "위": "Wi", "표": "Pyo", "명": "Myung", "기": "Ki",
    "반": "Ban", "라": "Ra", "왕": "Wang", "금": "Geum", "옥": "Ok",
    "육": "Yook", "인": "In", "맹": "Maeng", "제": "Je", "모": "Mo",
    "탁": "Tak", "국": "Kook", "어": "Eo", "은": "Eun", "편": "Pyun",
    "용": "Yong", "예": "Ye", "경": "Kyung", "봉": "Bong", "사": "Sa",
    "부": "Boo", "복": "Bok", "단": "Dan", "태": "Tae", "팽": "Paeng",
    "탄": "Tan", "피": "Pi", "빈": "Bin", "동": "Dong", "두": "Doo",
    "감": "Gam", "갈": "Gal", "간": "Gan", "견": "Gyun", "경": "Kyung",
    "계": "Gye", "구": "Koo", "국": "Kook", "궁": "Kung", "궉": "Gwok",
    "근": "Geun", "기": "Ki", "낭": "Nang", "내": "Nae", "녹": "Nok",
    "단": "Dan", "담": "Dam", "당": "Dang", "대": "Dae", "독": "Dok",
    "돈": "Don", "둔": "Dun", "마": "Ma", "만": "Man", "매": "Mae",
    "맥": "Maek", "묵": "Muk", "묘": "Myo", "미": "Mi", "박": "Park",
    "범": "Beom", "변": "Byun", "복": "Bok", "봉": "Bong", "비": "Bi",
    "빙": "Bing", "사": "Sa", "삼": "Sam", "상": "Sang", "서": "Seo",
    "석": "Seok", "선": "Sun", "설": "Seol", "섭": "Seop", "성": "Sung",
    "소": "So", "송": "Song", "수": "Soo", "순": "Soon", "승": "Seung",
    "시": "Shi", "신": "Shin", "심": "Shim", "아": "Ah", "안": "Ahn",
    "애": "Ae", "야": "Ya", "양": "Yang", "어": "Eo", "엄": "Um",
    "여": "Yeo", "연": "Yeon", "염": "Yeom", "엽": "Yeop", "영": "Young",
    "예": "Ye", "오": "Oh", "옥": "Ok", "온": "On", "옹": "Ong",
    "왕": "Wang", "용": "Yong", "우": "Woo", "운": "Woon", "원": "Won",
    "위": "Wi", "유": "Yoo", "육": "Yook", "윤": "Yoon", "은": "Eun",
    "음": "Eum", "이": "Lee", "인": "In", "임": "Lim", "장": "Jang",
    "전": "Jeon", "정": "Jung", "제": "Je", "조": "Cho", "종": "Jong",
    "좌": "Jwa", "주": "Joo", "지": "Ji", "진": "Jin", "차": "Cha",
    "창": "Chang", "채": "Chae", "천": "Cheon", "초": "Cho", "최": "Choi",
    "추": "Choo", "탁": "Tak", "탄": "Tan", "태": "Tae", "판": "Pan",
    "팽": "Paeng", "편": "Pyun", "평": "Pyung", "포": "Po", "표": "Pyo",
    "풍": "Pung", "피": "Pi", "필": "Pil", "하": "Ha", "한": "Han",
    "함": "Ham", "해": "Hae", "허": "Heo", "현": "Hyun", "형": "Hyung",
    "호": "Ho", "홍": "Hong", "화": "Hwa", "황": "Hwang", "후": "Hu",
}

SURNAMES_2CHAR = {
    "황보": "Hwangbo", "남궁": "Namkoong", "선우": "Sunwoo",
    "제갈": "Jegal", "사공": "Sagong", "서문": "Seomoon",
    "독고": "Dokgo", "동방": "Dongbang", "어금": "Eokeum",
    "장곡": "Janggok",
}


def _decompose(char: str) -> tuple[str, str, str] | None:
    code = ord(char)
    if not (HANGUL_START <= code <= HANGUL_END):
        return None
    offset = code - HANGUL_START
    i = offset // 588
    m = (offset % 588) // 28
    f = offset % 28
    return INITIALS[i], MEDIALS[m], FINALS[f]


def _romanize_syllable(char: str) -> str:
    parts = _decompose(char)
    if parts is None:
        return char
    initial, medial, final = parts
    return f"{initial}{medial}{final}"


def _phonetic(text: str) -> str:
    return "".join(_romanize_syllable(c) for c in text)


def _is_hangul(text: str) -> bool:
    return any(HANGUL_START <= ord(c) <= HANGUL_END for c in text)


def romanize_name(name: str | None) -> str:
    """Romanize a Korean name. Pass through non-Hangul input unchanged.

    Input is the full name as a single string. Handles both `김민준`-style (no space)
    and `김 민준`-style inputs. Compound surnames (황보, 남궁, ...) checked first.
    """
    if not name:
        return ""
    name = name.strip()
    if not _is_hangul(name):
        return name

    no_space = name.replace(" ", "")
    if len(no_space) >= 2 and no_space[:2] in SURNAMES_2CHAR:
        surname_en = SURNAMES_2CHAR[no_space[:2]]
        given = no_space[2:]
    elif no_space and no_space[0] in SURNAMES_1CHAR:
        surname_en = SURNAMES_1CHAR[no_space[0]]
        given = no_space[1:]
    else:
        return _phonetic(no_space).capitalize()

    given_en = _phonetic(given).capitalize() if given else ""
    return f"{surname_en} {given_en}".strip()


def romanize_full(first: str | None, last: str | None) -> str:
    """vcita stores Korean names typically all in `first_name`; `last_name` often null."""
    first_part = (first or "").strip()
    last_part = (last or "").strip()
    if first_part and last_part:
        combined = f"{first_part} {last_part}"
    else:
        combined = first_part or last_part
    return romanize_name(combined)
