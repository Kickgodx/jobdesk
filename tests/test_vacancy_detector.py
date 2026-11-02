import pytest

from jobdesk.extraction.vacancy_detector import compute_vacancy_score, is_not_vacancy_post


def test_hiring_post_scores():
    score, _, substantive = compute_vacancy_score(
        "Вакансия: Python developer remote, salary 3000-4000 USD, contact @hr_bot",
    )
    assert score >= 25
    assert substantive >= 8


def test_score_uses_100_point_scale():
    # Все признаки сразу — 100, как у вакансий HH; без контакта — 83 (10 из 12 сырых баллов).
    text = "Вакансия, company: Acme. Senior Python developer, remote, зарплата 300 000 руб, пишите @hr_bot"
    score, reason, substantive = compute_vacancy_score(text)
    assert (score, substantive) == (100, 83), reason


def test_contact_only_scores_on_100_point_scale():
    score, _, substantive = compute_vacancy_score("пишите @someone")
    assert (score, substantive) == (17, 0)


def test_resume_excluded_zero():
    score, reason, substantive = compute_vacancy_score("Ищу работу, приложил резюме")
    assert score == 0
    assert substantive == 0
    assert reason == "exclude_keywords"


def test_contact_only_high_total_low_substantive():
    _, _, substantive = compute_vacancy_score("что-то чат @gogetajob просто упомянули")
    assert substantive == 0


@pytest.mark.parametrize(
    "text",
    [
        "#resume #design Senior Product Designer с 7+ годами опыта, looking for a role",
        "#ИщуРаботу #ИщуРаботуРазработчик #React Frontend",
        "#CV #java #remote Senior Java Developer, seeking a role",
        "🚀 Senior Manual QA Engineer | #OpenToWork",
        "Подборка вакансий на 29.10.2026 ⚙️ Backend Go Developer (до 1 200 000 KZT)",
        "Подборка свежих вакансий: Go, Python",
        "Ещё 14 вакансий дня · Разработка · 20.10 DevOps / SRE",
        "Дайджест вакансий в зарубежных стартапах",
        "#дайджест 🔥Разработчики Agentic Product Engineer / Altery Remote. Full-Stack Engineer / ElevenLabs",
    ],
)
def test_seeker_and_digest_posts_are_not_vacancies(text):
    assert is_not_vacancy_post(text)
    assert compute_vacancy_score(text) == (0, "exclude_keywords", 0)


@pytest.mark.parametrize(
    "text",
    [
        # Обычные фразы настоящих вакансий — раньше пост отбрасывался ещё до LLM.
        "Требуется Python-разработчик. Присылайте резюме @hr",
        "Python developer, remote, от 300k. Обучение за счёт компании",
        "Тестировщик junior+ / middle. Оплачиваем курсы и обучение",
        "QA Lead. Менторство и школа тестирования внутри компании",
        # #CV = computer vision, рядом хэштег вакансии.
        "Senior CV/ML Engineer #вакансия #ML #CV #vacancy remote 5000$",
    ],
)
def test_vacancy_with_learning_words_passes(text):
    assert not is_not_vacancy_post(text)
    score, reason, _ = compute_vacancy_score(text)
    assert reason != "exclude_keywords" and score > 0


@pytest.mark.parametrize(
    "text",
    ["Курс Python для начинающих, обучение с ментором", "Онлайн-школа программирования: скидка на курс"],
)
def test_course_ads_without_role_excluded(text):
    assert compute_vacancy_score(text)[1] == "exclude_keywords"
