import asyncio
from datetime import date, timedelta
import os
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy import Column, Integer, Float, Date, BigInteger, String, select, func
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# --- Конфигурация ---
TOKEN = os.getenv("TELEGRAM_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://primer:primer@db:5432/economoney_db")
DAILY_LIMIT = 2000

Base = declarative_base()
engine = create_async_engine(DATABASE_URL, echo=False)
async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# --- Модели ---
class Expense(Base):
    __tablename__ = "expenses"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, nullable=False)
    amount = Column(Float, nullable=False)
    date = Column(Date, nullable=False, default=date.today)
    category = Column(String, nullable=True)


class Balance(Base):
    __tablename__ = "balances"
    id = Column(Integer, primary_key=True)
    user_id = Column(BigInteger, nullable=False)
    date = Column(Date, nullable=False)
    balance = Column(Float, nullable=False)


# --- Клавиатуры ---
menu_kb = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="📅 День", callback_data="stats_day"),
        InlineKeyboardButton(text="📈 Неделя", callback_data="stats_week"),
        InlineKeyboardButton(text="📊 Месяц", callback_data="stats_month")
    ],
    [
        InlineKeyboardButton(text="💰 Пополнить бюджет", callback_data="add_budget")
    ]
])

categories_kb = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="🛒 Продукты", callback_data="cat_Продукты"),
        InlineKeyboardButton(text="🎉 Развлечения", callback_data="cat_Развлечения")
    ],
    [
        InlineKeyboardButton(text="🍔 Доставка/Рестораны", callback_data="cat_Доставка/Рестораны"),
        InlineKeyboardButton(text="🚗 Транспорт", callback_data="cat_Транспорт"),
    ],
    [
        InlineKeyboardButton(text="🧾 Прочее", callback_data="cat_Прочее")
    ]
])

# --- Временное хранилище ---
pending_expenses = {}  # {user_id: amount}
awaiting_budget_add = set()


# --- Функции работы с балансом ---
async def get_balance(session: AsyncSession, user_id: int) -> float:
    today = date.today()

    # Проверяем, есть ли баланс на сегодня
    result = await session.execute(select(Balance).filter_by(user_id=user_id, date=today))
    balance_today = result.scalars().first()
    if balance_today:
        return balance_today.balance

    # Находим последний известный баланс пользователя
    result = await session.execute(
        select(Balance)
        .filter(Balance.user_id == user_id)
        .order_by(Balance.date.desc())
        .limit(1)
    )
    last_balance = result.scalars().first()

    if not last_balance:
        # Если записей нет — создаём первую запись с DAILY_LIMIT
        new_entry = Balance(user_id=user_id, date=today, balance=DAILY_LIMIT)
        session.add(new_entry)
        await session.commit()
        return DAILY_LIMIT

    # Сколько дней прошло с момента последнего обновления
    days_passed = (today - last_balance.date).days

    if days_passed <= 0:
        # Если всё в порядке — возвращаем последний баланс
        return last_balance.balance

    # Добавляем лимит за каждый пропущенный день
    new_balance_value = last_balance.balance + (days_passed * DAILY_LIMIT)

    # Создаём запись на сегодня с новым балансом
    new_entry = Balance(user_id=user_id, date=today, balance=new_balance_value)
    session.add(new_entry)
    await session.commit()

    return new_balance_value


async def update_balance(session: AsyncSession, user_id: int, new_balance: float):
    today = date.today()
    await session.execute(
        Balance.__table__.update()
        .where(Balance.user_id == user_id, Balance.date == today)
        .values(balance=new_balance)
    )
    await session.commit()


async def add_expense(session: AsyncSession, user_id: int, amount: float, category: str):
    today = date.today()
    balance = await get_balance(session, user_id)
    new_balance = balance - amount

    session.add(Expense(user_id=user_id, amount=amount, date=today, category=category))
    await update_balance(session, user_id, new_balance)
    return new_balance


async def add_to_budget(session: AsyncSession, user_id: int, amount: float):
    """
    Изменяет текущий баланс пользователя:
    +X → добавляет X рублей
    -X → уменьшает на X рублей
    """
    current_balance = await get_balance(session, user_id)
    new_balance = current_balance + amount  # amount может быть отрицательным

    # Обновляем баланс в таблице
    await update_balance(session, user_id, new_balance)
    await session.commit()
    return new_balance


async def get_stats(session: AsyncSession, user_id: int, period: str):
    today = date.today()
    if period == "day":
        start_date = today
    elif period == "week":
        start_date = today - timedelta(days=7)
    elif period == "month":
        start_date = today.replace(day=1)
    else:
        start_date = today

    query = (
        select(Expense.category, func.sum(Expense.amount))
        .filter(Expense.user_id == user_id, Expense.date >= start_date)
        .group_by(Expense.category)
    )

    result = await session.execute(query)
    return result.all()


async def get_weekly_stats(session: AsyncSession, user_id: int):
    """Возвращает расходы и баланс по каждому дню за последнюю неделю."""
    today = date.today()
    start_date = today - timedelta(days=6)

    # --- Расходы по дням и категориям ---
    query = (
        select(Expense.date, Expense.category, func.sum(Expense.amount))
        .filter(Expense.user_id == user_id, Expense.date >= start_date)
        .group_by(Expense.date, Expense.category)
        .order_by(Expense.date)
    )
    result = await session.execute(query)
    rows = result.all()

    daily_stats = {}
    for d, cat, amount in rows:
        if d not in daily_stats:
            daily_stats[d] = {}
        daily_stats[d][cat or "Без категории"] = amount

    # --- Балансы за эти же дни ---
    balance_query = (
        select(Balance.date, Balance.balance)
        .filter(Balance.user_id == user_id, Balance.date >= start_date)
        .order_by(Balance.date)
    )
    balances_result = await session.execute(balance_query)
    balances = {d: b for d, b in balances_result.all()}

    total_sum = sum(sum(cats.values()) for cats in daily_stats.values())

    return daily_stats, balances, total_sum


# --- Telegram логика ---
bot = Bot(token=TOKEN)
dp = Dispatcher()


@dp.message(F.text == "/start")
async def start_cmd(message: Message):
    async with async_session() as session:
        current_balance = await get_balance(session, message.from_user.id)

    await message.answer(
        "👋 Привет! Я помогу отслеживать твои расходы.\n"
        f"На сегодня у тебя {current_balance:.2f} ₽.\n\n"
        "Отправь сумму, которую потратил, и выбери категорию.",
        reply_markup=menu_kb
    )


@dp.message(F.text.regexp(r"^-?\d+(\.\d+)?$"))
async def handle_amount(message: Message):
    user_id = message.from_user.id

    # Проверим, ожидается ли пополнение бюджета
    if user_id in awaiting_budget_add:
        amount = float(message.text)
        async with async_session() as session:
            new_balance = await add_to_budget(session, user_id, amount)
        awaiting_budget_add.remove(user_id)
        sign_text = "пополнен" if amount >= 0 else "уменьшен"
        await message.answer(
            f"✅ Бюджет {sign_text} на {abs(amount):.2f} ₽.\n"
            f"Текущий баланс: {new_balance:.2f} ₽",
            reply_markup=menu_kb
        )
        return

    # Обычная трата
    pending_expenses[user_id] = float(message.text)
    await message.answer("Выбери категорию:", reply_markup=categories_kb)


@dp.callback_query(F.data.startswith("cat_"))
async def handle_category(call: CallbackQuery):
    user_id = call.from_user.id
    category = call.data.split("_", 1)[1]
    amount = pending_expenses.pop(user_id, None)

    if amount is None:
        await call.answer("Нет ожидающей суммы 😅", show_alert=True)
        return

    async with async_session() as session:
        new_balance = await add_expense(session, user_id, amount, category)

    await call.message.answer(
        f"💸 Потратил {amount:.2f} ₽ на {category}.\n"
        f"Остаток на сегодня: {new_balance:.2f} ₽",
        reply_markup=menu_kb
    )
    await call.answer()


@dp.callback_query(F.data == "add_budget")
async def handle_add_budget(call: CallbackQuery):
    awaiting_budget_add.add(call.from_user.id)
    await call.message.answer("💰 Введи сумму, на которую хочешь пополнить сегодняшний бюджет:")
    await call.answer()


@dp.callback_query(F.data.startswith("stats_"))
async def stats_callback(call: CallbackQuery):
    period = call.data.split("_")[1]
    names = {"day": "день", "week": "неделю", "month": "месяц"}

    async with async_session() as session:
        # ---- 📈 Статистика за неделю ----
        if period == "week":
            daily_stats, balances, total_sum = await get_weekly_stats(session, call.from_user.id)
            if not daily_stats and not balances:
                await call.message.answer("📊 За неделю расходов не найдено.", reply_markup=menu_kb)
                await call.answer()
                return

            lines = [f"📆 Статистика за последнюю неделю:\n"]
            sorted_days = sorted(set(daily_stats.keys()) | set(balances.keys()))

            for d in sorted_days:
                cats = daily_stats.get(d, {})
                lines.append(f"📅 {d.strftime('%d.%m.%Y')}:")
                if cats:
                    for cat, amount in cats.items():
                        lines.append(f"   • {cat}: {amount:.2f} ₽")
                else:
                    lines.append("   • Нет расходов")
                day_total = sum(cats.values()) if cats else 0
                lines.append(f"   💵 Потрачено за день: {day_total:.2f} ₽")

                if d in balances:
                    lines.append(f"   💰 Баланс на конец дня: {balances[d]:.2f} ₽\n")
                else:
                    lines.append("   💰 Баланс не найден\n")

            lines.append(f"💵 Потрачено за неделю: {total_sum:.2f} ₽")
            await call.message.answer("\n".join(lines), reply_markup=menu_kb)
            await call.answer()
            return

        # ---- 📅 Статистика за день / месяц ----
        stats = await get_stats(session, call.from_user.id, period)
        lines = [f"📊 Статистика за {names[period]}:\n"]
        total = 0
        for category, amount in stats:
            lines.append(f"• {category or 'Без категории'} — {amount:.2f} ₽")
            total += amount
        lines.append(f"\n💵 Потрачено: {total:.2f} ₽")

        # 🔹 Добавляем баланс только если день
        if period == "day":
            today = date.today()
            balance_query = await session.execute(
                select(Balance.balance).filter_by(user_id=call.from_user.id, date=today)
            )
            balance_today = balance_query.scalar()

            # 💡 Если нет записи — пробуем вычислить вручную
            if balance_today is None:
                # 1. Найти последний известный баланс
                last_balance_query = await session.execute(
                    select(Balance.date, Balance.balance)
                    .filter(Balance.user_id == call.from_user.id)
                    .order_by(Balance.date.desc())
                    .limit(1)
                )
                last_balance_row = last_balance_query.first()

                if last_balance_row:
                    last_date, last_balance = last_balance_row
                    days_passed = (today - last_date).days
                    # 2. Сколько лимита добавилось с тех пор
                    restored_balance = last_balance + days_passed * DAILY_LIMIT
                else:
                    restored_balance = DAILY_LIMIT  # если вообще нет записей

                # 3. Вычитаем траты за сегодня
                expenses_query = await session.execute(
                    select(func.sum(Expense.amount))
                    .filter(Expense.user_id == call.from_user.id, Expense.date == today)
                )
                spent_today = expenses_query.scalar() or 0
                balance_today = restored_balance - spent_today

            lines.append(f"💰 Баланс на конец дня: {balance_today:.2f} ₽")

        await call.message.answer("\n".join(lines), reply_markup=menu_kb)
        await call.answer()


# --- Запуск ---
async def main():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())