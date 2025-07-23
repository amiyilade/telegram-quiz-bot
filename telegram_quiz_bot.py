import os
import json
import random
import asyncio
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
QUESTIONS_FILE = "questions.json"

# Global state
questions_data = []
question_pool = {}
active_players = []
player_scores = {}
answered_questions = set()
current_turn_index = 0
in_progress = False
message_cache = {}
group_chat_id = None

# Load questions from JSON
def load_questions():
    with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

questions_data = load_questions()
question_pool = {str(i+1): q for i, q in enumerate(questions_data)}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome to the Bible Study Quiz! Type /join to participate.")

async def join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    global in_progress
    if user.id not in active_players and not in_progress:
        active_players.append(user.id)
        player_scores[user.id] = 0
        await update.message.reply_text(f"{user.first_name} has joined the quiz!")
    elif in_progress:
        await update.message.reply_text("The quiz is already in progress. Wait for the next one.")

async def begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global in_progress, current_turn_index, answered_questions, group_chat_id
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Only the host can start the quiz.")
        return

    if not active_players:
        await update.message.reply_text("No players have joined.")
        return

    group_chat_id = update.effective_chat.id
    in_progress = True
    current_turn_index = 0
    answered_questions = set()

    await context.bot.send_message(chat_id=group_chat_id, text="Quiz starting now!")
    await next_turn(context)

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global in_progress, active_players, player_scores, current_turn_index, answered_questions
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Only the host can stop the quiz.")
        return

    in_progress = False
    active_players.clear()
    player_scores.clear()
    answered_questions.clear()
    current_turn_index = 0
    await context.bot.send_message(chat_id=group_chat_id, text="Quiz has been stopped.")

async def next_turn(context: ContextTypes.DEFAULT_TYPE):
    global current_turn_index

    if current_turn_index >= len(active_players):
        await end_quiz(context)
        return

    user_id = active_players[current_turn_index]
    user = await context.bot.get_chat(user_id)
    available = [k for k in question_pool if k not in answered_questions]

    if not available:
        await end_quiz(context)
        return

    mention = f"[{user.first_name}](tg://user?id={user.id})"
    await context.bot.send_message(
        chat_id=group_chat_id,
        text=f"Your turn, {mention}! Pick a number from {available}",
        parse_mode="Markdown"
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global current_turn_index

    if not in_progress or update.effective_user.id != active_players[current_turn_index]:
        return  # Ignore messages from non-players or out-of-turn users

    chosen = update.message.text.strip()
    if chosen not in question_pool or chosen in answered_questions:
        await context.bot.send_message(chat_id=group_chat_id, text="Invalid or already used number. Try again.")
        return

    question = question_pool[chosen]
    answered_questions.add(chosen)

    if question["type"] == "mcq":
        question_text = f"{question['question']}\nOptions:\n" + "\n".join(question["options"])
        msg = await context.bot.send_message(chat_id=group_chat_id, text=question_text)
        context.user_data["current_answer"] = question["answer"]
        context.user_data["responding_to"] = update.effective_user.id
        await show_timer(context, group_chat_id, msg.message_id, 30, question_text)

        await asyncio.sleep(31)
        await check_mcq_answer(update, context)

    elif question["type"] == "paragraph":
        question_text = f"{question['question']} (You have 30 seconds to respond.)"
        msg = await context.bot.send_message(chat_id=group_chat_id, text=question_text)
        context.user_data["manual_review"] = True
        context.user_data["responding_to"] = update.effective_user.id
        await show_timer(context, group_chat_id, msg.message_id, 30, question_text)
        await asyncio.sleep(31)

    current_turn_index += 1
    await next_turn(context)

async def check_mcq_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    answer = context.user_data.get("current_answer")
    response = update.message.text.strip()

    if response.lower() == answer.lower():
        player_scores[user_id] += 1
        await context.bot.send_message(chat_id=group_chat_id, text=f"✅ {user.first_name}, that's correct!")
    else:
        await context.bot.send_message(chat_id=group_chat_id, text=f"❌ {user.first_name}, that's incorrect. The correct answer was: {answer}")

async def show_timer(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, duration: int, original_text: str):
    for remaining in range(duration, 0, -10):
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=f"{original_text}\n\n⏳ {remaining} seconds left..."
        )
        await asyncio.sleep(10)
    await context.bot.edit_message_text(
        chat_id=chat_id,
        message_id=message_id,
        text=f"{original_text}\n\n⏰ Time's up!"
    )

async def end_quiz(context: ContextTypes.DEFAULT_TYPE):
    global in_progress
    in_progress = False
    leaderboard = sorted(player_scores.items(), key=lambda x: x[1], reverse=True)
    result = ["\ud83c\udfc6 Final Leaderboard:"]
    for i, (uid, score) in enumerate(leaderboard):
        name = (await context.bot.get_chat(uid)).first_name
        result.append(f"{i+1}. {name} — {score} point(s)")
    await context.bot.send_message(chat_id=group_chat_id, text="\n".join(result))

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("join", join))
    app.add_handler(CommandHandler("begin", begin))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    app.run_polling()
