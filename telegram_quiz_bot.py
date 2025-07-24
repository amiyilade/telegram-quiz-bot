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
waiting_for_mcq_answer = False  # New state variable
mcq_timer_task = None  # To store the timer task

# Global review state (shared across all users)
review_state = {
    "awaiting_admin_review": False,
    "responding_user_id": None,
    "paragraph_answer": None
}

# Load questions from JSON
def load_questions():
    with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

questions_data = load_questions()
question_pool = {str(i+1): q for i, q in enumerate(questions_data)}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    await update.message.reply_text("Welcome to the Bible Study Quiz! Type /join to participate.")

async def join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    user = update.effective_user
    global in_progress
    if user.id not in active_players and not in_progress:
        active_players.append(user.id)
        player_scores[user.id] = 0
        await update.message.reply_text(f"{user.first_name} has joined the quiz!")
    elif in_progress:
        await update.message.reply_text("The quiz is already in progress. Wait for the next one.")

async def begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global in_progress, current_turn_index, answered_questions, group_chat_id, review_state
    if not update.message or not update.effective_user:
        return
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
    
    # Reset review state
    review_state = {
        "awaiting_admin_review": False,
        "responding_user_id": None,
        "paragraph_answer": None
    }

    await context.bot.send_message(chat_id=group_chat_id, text="Quiz starting now!")
    await next_turn(context)

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global in_progress, active_players, player_scores, current_turn_index, answered_questions, waiting_for_mcq_answer, mcq_timer_task, review_state
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Only the host can stop the quiz.")
        return

    # Cancel any running timers
    if mcq_timer_task and not mcq_timer_task.done():
        mcq_timer_task.cancel()
    
    if context.user_data.get("paragraph_timer_task") and not context.user_data["paragraph_timer_task"].done():
        context.user_data["paragraph_timer_task"].cancel()

    in_progress = False
    waiting_for_mcq_answer = False
    context.user_data.clear()  # Clear all user data
    active_players.clear()
    player_scores.clear()
    answered_questions.clear()
    current_turn_index = 0
    
    # Reset review state
    review_state = {
        "awaiting_admin_review": False,
        "responding_user_id": None,
        "paragraph_answer": None
    }
    
    await context.bot.send_message(chat_id=group_chat_id, text="Quiz has been stopped.")

async def next_turn(context: ContextTypes.DEFAULT_TYPE):
    global current_turn_index

    if current_turn_index >= len(active_players):
        # Show leaderboard after each complete round
        await show_leaderboard(context, is_final=False)
        
        # Check if we should end the quiz or continue
        if len(answered_questions) >= len(question_pool):
            await end_quiz(context)
            return
        
        # Reset for next round
        current_turn_index = 0

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
    global current_turn_index, waiting_for_mcq_answer, mcq_timer_task, review_state

    if not update.message or not update.effective_user:
        return

    if not in_progress:
        return

    # Handle MCQ answers
    if waiting_for_mcq_answer and update.effective_user.id == context.user_data.get("responding_to"):
        # Cancel the timer since user answered
        if mcq_timer_task and not mcq_timer_task.done():
            mcq_timer_task.cancel()
        
        await check_mcq_answer(update, context)
        waiting_for_mcq_answer = False
        current_turn_index += 1
        await next_turn(context)
        return

    # Handle paragraph answers
    if context.user_data.get("waiting_for_paragraph") and update.effective_user.id == context.user_data.get("responding_to"):
        # Cancel the paragraph timer since user answered
        if context.user_data.get("paragraph_timer_task") and not context.user_data["paragraph_timer_task"].done():
            context.user_data["paragraph_timer_task"].cancel()
        
        # Store answer in global review state instead of user_data
        review_state["paragraph_answer"] = update.message.text
        review_state["responding_user_id"] = update.effective_user.id
        review_state["awaiting_admin_review"] = True
        
        context.user_data["waiting_for_paragraph"] = False
        
        # Send to admin for review
        await context.bot.send_message(
            chat_id=group_chat_id,
            text=f"Admin, please review {update.effective_user.first_name}'s answer: \"{update.message.text}\"\n\nReply with /approve or /reject."
        )
        return

    # Handle question selection (only if it's the user's turn and we're not waiting for an answer)
    if update.effective_user.id != active_players[current_turn_index] or waiting_for_mcq_answer:
        return

    chosen = update.message.text.strip()
    if chosen not in question_pool or chosen in answered_questions:
        await context.bot.send_message(chat_id=group_chat_id, text="Invalid or already used number. Try again.")
        return

    question = question_pool[chosen]
    answered_questions.add(chosen)

    if question["type"] == "mcq":
        options = question["options"]
        lettered_options = [f"{chr(97 + i)}) {opt}" for i, opt in enumerate(options)]  # a) option1, b) option2, ...
        question_text = f"{question['question']}\nOptions:\n" + "\n".join(lettered_options)
        msg = await context.bot.send_message(chat_id=group_chat_id, text=question_text)
        
        # Set up answer checking data
        context.user_data["current_answer"] = question["answer"].strip().lower()
        context.user_data["options_map"] = {
            chr(97 + i): opt.strip().lower() for i, opt in enumerate(options)
        }
        # Also include uppercase letters for user input flexibility
        context.user_data["options_map"].update({
            chr(65 + i): opt.strip().lower() for i, opt in enumerate(options)
        })
        context.user_data["responding_to"] = update.effective_user.id
        
        # Set state to wait for MCQ answer
        waiting_for_mcq_answer = True
        
        # Start timer
        mcq_timer_task = asyncio.create_task(handle_mcq_timeout(context, msg.message_id, question_text))

    elif question["type"] == "paragraph":
        question_text = f"{question['question']} (You have 30 seconds to respond.)"
        msg = await context.bot.send_message(chat_id=group_chat_id, text=question_text)
        
        # Set up paragraph answer waiting
        context.user_data["waiting_for_paragraph"] = True
        context.user_data["responding_to"] = update.effective_user.id
        
        # Start paragraph timer
        context.user_data["paragraph_timer_task"] = asyncio.create_task(
            handle_paragraph_timeout(context, msg.message_id, question_text)
        )

async def handle_paragraph_timeout(context: ContextTypes.DEFAULT_TYPE, message_id: int, original_text: str):
    """Handle paragraph timeout"""
    global current_turn_index
    try:
        await show_timer(context, group_chat_id, message_id, 30, original_text)
        await asyncio.sleep(1)  # Small delay to ensure timer shows "Time's up!"
        
        # If we're still waiting for an answer, time has run out
        if context.user_data.get("waiting_for_paragraph"):
            context.user_data["waiting_for_paragraph"] = False
            user_id = context.user_data.get("responding_to")
            if user_id:
                user = await context.bot.get_chat(user_id)
                await context.bot.send_message(
                    chat_id=group_chat_id, 
                    text=f"⏰ Time's up, {user.first_name}! Moving to next turn."
                )
            
            current_turn_index += 1
            await next_turn(context)
    except asyncio.CancelledError:
        # Timer was cancelled because user answered in time
        pass

async def handle_mcq_timeout(context: ContextTypes.DEFAULT_TYPE, message_id: int, original_text: str):
    """Handle MCQ timeout"""
    global waiting_for_mcq_answer, current_turn_index
    try:
        await show_timer(context, group_chat_id, message_id, 30, original_text)
        await asyncio.sleep(1)  # Small delay to ensure timer shows "Time's up!"
        
        # If we're still waiting for an answer, time has run out
        if waiting_for_mcq_answer:
            waiting_for_mcq_answer = False
            user_id = context.user_data.get("responding_to")
            if user_id:
                user = await context.bot.get_chat(user_id)
                correct_answer = context.user_data.get("current_answer", "")
                await context.bot.send_message(
                    chat_id=group_chat_id, 
                    text=f"⏰ Time's up, {user.first_name}! The correct answer was: {correct_answer}"
                )
            
            current_turn_index += 1
            await next_turn(context)
    except asyncio.CancelledError:
        # Timer was cancelled because user answered in time
        pass

async def check_mcq_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    correct_answer = context.user_data.get("current_answer", "").strip().lower()
    options_map = context.user_data.get("options_map", {})
    user_response = update.message.text.strip()

    # Normalize user response - remove whitespace and convert to lowercase
    normalized_response = user_response.replace(" ", "").lower()
    
    # Check if user typed a letter (a, b, c, d)
    if normalized_response in options_map:
        interpreted_response = options_map[normalized_response]
    else:
        # User typed the actual answer, normalize it
        interpreted_response = normalized_response

    if interpreted_response == correct_answer:
        player_scores[user_id] += 1
        await context.bot.send_message(chat_id=group_chat_id, text=f"✅ {user.first_name}, that's correct!")
    else:
        await context.bot.send_message(chat_id=group_chat_id, text=f"❌ {user.first_name}, that's incorrect. The correct answer was: {correct_answer}")

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

async def show_leaderboard(context: ContextTypes.DEFAULT_TYPE, is_final=False):
    """Show current leaderboard"""
    if not group_chat_id:
        return  # Can't show leaderboard if no group chat is set
        
    leaderboard = sorted(player_scores.items(), key=lambda x: x[1], reverse=True)
    title = "🏆 Final Leaderboard:" if is_final else "📊 Current Leaderboard:"
    result = [title]
    for i, (uid, score) in enumerate(leaderboard):
        name = (await context.bot.get_chat(uid)).first_name
        result.append(f"{i+1}. {name} — {score} point(s)")
    await context.bot.send_message(chat_id=group_chat_id, text="\n".join(result))

async def end_quiz(context: ContextTypes.DEFAULT_TYPE):
    global in_progress
    in_progress = False
    await show_leaderboard(context, is_final=True)

async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global review_state, current_turn_index
    
    if not update.message or not update.effective_user:
        return
    
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Only the admin can approve answers.")
        return
    
    if not review_state["awaiting_admin_review"]:
        await update.message.reply_text("No answer is currently awaiting review.")
        return
        
    user_id = review_state["responding_user_id"]
    if not user_id:
        await update.message.reply_text("Error: No user found for this review.")
        return
        
    player_scores[user_id] += 1
    user = await context.bot.get_chat(user_id)
    await context.bot.send_message(chat_id=group_chat_id, text=f"✅ {user.first_name}'s answer has been approved.")
    
    # Clear review state
    review_state = {
        "awaiting_admin_review": False,
        "responding_user_id": None,
        "paragraph_answer": None
    }
    
    # Move to next turn
    current_turn_index += 1
    await next_turn(context)

async def reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global review_state, current_turn_index
    
    if not update.message or not update.effective_user:
        return
    
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("Only the admin can reject answers.")
        return
    
    if not review_state["awaiting_admin_review"]:
        await update.message.reply_text("No answer is currently awaiting review.")
        return
        
    user_id = review_state["responding_user_id"]
    if not user_id:
        await update.message.reply_text("Error: No user found for this review.")
        return
        
    user = await context.bot.get_chat(user_id)
    await context.bot.send_message(chat_id=group_chat_id, text=f"❌ {user.first_name}'s answer has been rejected.")
    
    # Clear review state
    review_state = {
        "awaiting_admin_review": False,
        "responding_user_id": None,
        "paragraph_answer": None
    }
    
    # Move to next turn
    current_turn_index += 1
    await next_turn(context)

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("join", join))
    app.add_handler(CommandHandler("begin", begin))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("approve", approve))
    app.add_handler(CommandHandler("reject", reject))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    app.run_polling()