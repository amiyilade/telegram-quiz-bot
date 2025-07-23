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

# Load questions from JSON
def load_questions():
    with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

questions_data = load_questions()
question_pool = {str(i+1): q for i, q in enumerate(questions_data)}

active_players = []
player_scores = {}
answered_questions = set()
current_turn_index = 0
in_progress = False
message_cache = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome to the Bible Study Quiz! Type /join to participate.")

async def join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in active_players and not in_progress:
        active_players.append(user.id)
        player_scores[user.id] = 0
        await update.message.reply_text(f"{user.first_name} has joined the quiz!")
    elif in_progress:
        await update.message.reply_text("The quiz is already in progress. Wait for the next one.")

async def begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global in_progress, current_turn_index, answered_questions
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Only the host can start the quiz.")
        return

    if not active_players:
        await update.message.reply_text("No players have joined.")
        return

    in_progress = True
    current_turn_index = 0
    answered_questions = set()
    await update.message.reply_text("Quiz starting now!")
    await next_turn(context)

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

    await context.bot.send_message(chat_id=user_id, text=f"Your turn, {user.first_name}! Pick a number from {available}")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global current_turn_index

    user = update.effective_user
    if user.id != active_players[current_turn_index]:
        return

    chosen = update.message.text.strip()
    if chosen not in question_pool or chosen in answered_questions:
        await update.message.reply_text("Invalid or already used number. Try again.")
        return

    question = question_pool[chosen]
    answered_questions.add(chosen)

    if question["type"] == "mcq":
        msg = await update.message.reply_text(f"{question['question']} Options:" + "\n".join(question["options"]))
        await show_timer(context, update.effective_chat.id, msg.message_id, 30)
        context.user_data["current_answer"] = question["answer"]
    elif question["type"] == "paragraph":
        msg = await update.message.reply_text(f"{question['question']} (You have 30 seconds to respond.)")
        await show_timer(context, update.effective_chat.id, msg.message_id, 30)
        context.user_data["manual_review"] = True

    current_turn_index += 1
    await asyncio.sleep(31)
    await next_turn(context)

async def show_timer(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, duration: int):
    for remaining in range(duration, 0, -10):
        await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"⏳ {remaining} seconds left...")
        await asyncio.sleep(10)
    await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text="⏰ Time's up!")

async def end_quiz(context: ContextTypes.DEFAULT_TYPE):
    global in_progress
    in_progress = False
    leaderboard = sorted(player_scores.items(), key=lambda x: x[1], reverse=True)
    result = ["🏆 Final Leaderboard:"]
    for i, (uid, score) in enumerate(leaderboard):
        name = (await context.bot.get_chat(uid)).first_name
        result.append(f"{i+1}. {name} — {score} point(s)")
    for pid in active_players:
        await context.bot.send_message(chat_id=pid, text="\n".join(result))

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("join", join))
    app.add_handler(CommandHandler("begin", begin))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    app.run_polling()