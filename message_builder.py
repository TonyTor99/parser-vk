from datetime import datetime
from random import choice, randint, uniform


def build_fake_match_message() -> str:
    now = datetime.now().strftime("%d.%m.%Y, %H:%M")

    fixtures = [
        ("Caravel", "Atlantico Deportivo", "Aruba. Division di Honor"),
        ("Rembas Resavica", "Sloga Leskovac", "Serbia. Serbian League East"),
        ("Zeleznicar Lajkovac", "Jedinstvo Putevi", "Serbia. Serbian League West"),
    ]
    home_team, away_team, league = choice(fixtures)

    bet_types = [
        "Основная игра. П1",
        "Основная игра. ФОРА1 (0)",
        "Основная игра. Двойной шанс 1X",
    ]
    bet_type = choice(bet_types)

    odds = round(uniform(1.25, 4.80), 2)
    home_score = randint(0, 4)
    away_score = randint(0, 4)
    stake = 1000
    payout = int(stake * odds)

    score_line = f"{home_score}:{away_score}"
    return (
        "Статистика матча\n"
        f"{now}\n"
        f"{home_team} - {away_team}\n"
        f"{league}\n"
        "------------------------------\n"
        f"Ставка: {bet_type}\n"
        f"Коэффициент: {odds:.2f}\n"
        f"Счёт: {score_line}\n"
        f"Сумма: {stake} ₽\n"
        f"Выплата: {payout} ₽\n"
        "Статус: TEST_MODE_NO_PARSER"
    )
