import os
import json
import random
import asyncio
import pickle
from datetime import datetime
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
# Backup admin IDs - comma separated in environment variable
BACKUP_ADMIN_IDS = [int(x.strip()) for x in os.getenv("BACKUP_ADMIN_IDS", "").split(",") if x.strip()]
ALL_ADMIN_IDS = [ADMIN_ID] + BACKUP_ADMIN_IDS

QUESTIONS_FILE = "questions.json"
STATE_FILE = "game_state.pkl"

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
waiting_for_mcq_answer = False
mcq_timer_task = None

# Tiebreaker state
tiebreaker_state = {
    "in_progress": False,
    "tied_players": [],
    "current_phase": None,  # "speed_round" or "paragraph"
    "speed_round_question": None,
    "waiting_for_speed_answer": False,
    "first_responder": None,
    "speed_timer_task": None
}

# Global review state (shared across all users)
review_state = {
    "awaiting_admin_review": False,
    "responding_user_id": None,
    "paragraph_answer": None
}

def is_admin(user_id):
    """Check if user is an admin"""
    return user_id in ALL_ADMIN_IDS

def save_game_state():
    """Save current game state to file"""
    state = {
        "active_players": active_players,
        "player_scores": player_scores,
        "answered_questions": list(answered_questions),
        "current_turn_index": current_turn_index,
        "in_progress": in_progress,
        "group_chat_id": group_chat_id,
        "waiting_for_mcq_answer": waiting_for_mcq_answer,
        "tiebreaker_state": tiebreaker_state.copy(),
        "review_state": review_state.copy(),
        "timestamp": datetime.now().isoformat()
    }
    try:
        with open(STATE_FILE, "wb") as f:
            pickle.dump(state, f)
    except Exception as e:
        print(f"Error saving game state: {e}")

def load_game_state():
    """Load game state from file"""
    global active_players, player_scores, answered_questions, current_turn_index
    global in_progress, group_chat_id, waiting_for_mcq_answer, tiebreaker_state, review_state
    
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "rb") as f:
                state = pickle.load(f)
            
            active_players = state.get("active_players", [])
            player_scores = state.get("player_scores", {})
            answered_questions = set(state.get("answered_questions", []))
            current_turn_index = state.get("current_turn_index", 0)
            in_progress = state.get("in_progress", False)
            group_chat_id = state.get("group_chat_id", None)
            waiting_for_mcq_answer = state.get("waiting_for_mcq_answer", False)
            tiebreaker_state.update(state.get("tiebreaker_state", {}))
            review_state.update(state.get("review_state", {}))
            
            print(f"Game state loaded from {state.get('timestamp', 'unknown time')}")
            return True
    except Exception as e:
        print(f"Error loading game state: {e}")
    return False

# Load questions from JSON
def load_questions():
    with open(QUESTIONS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def build_regular_question_pool():
    """Build question pool excluding tiebreaker questions"""
    regular_questions = [q for q in questions_data if not q.get("is_tiebreaker", False)]
    return {str(i+1): q for i, q in enumerate(regular_questions)}

questions_data = load_questions()
question_pool = build_regular_question_pool()

# Load game state on startup
load_game_state()

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
        save_game_state()
        await update.message.reply_text(f"{user.first_name} has joined the quiz!")
    elif in_progress:
        await update.message.reply_text("The quiz is already in progress. Wait for the next one.")

async def begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global in_progress, current_turn_index, answered_questions, group_chat_id, review_state, tiebreaker_state, question_pool
    if not update.message or not update.effective_user:
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Only admins can start the quiz.")
        return

    if not active_players:
        await update.message.reply_text("No players have joined.")
        return

    # Rebuild question pool to exclude tiebreaker questions
    question_pool = build_regular_question_pool()
    
    if not question_pool:
        await update.message.reply_text("No regular questions available! Please add non-tiebreaker questions to the quiz.")
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
    
    # Reset tiebreaker state
    tiebreaker_state = {
        "in_progress": False,
        "tied_players": [],
        "current_phase": None,
        "speed_round_question": None,
        "waiting_for_speed_answer": False,
        "first_responder": None,
        "speed_timer_task": None
    }

    save_game_state()
    await context.bot.send_message(chat_id=group_chat_id, text="Quiz starting now!")
    await next_turn(context)

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global in_progress, active_players, player_scores, current_turn_index, answered_questions
    global waiting_for_mcq_answer, mcq_timer_task, review_state, tiebreaker_state
    
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Only admins can stop the quiz.")
        return

    # Cancel any running timers
    if mcq_timer_task and not mcq_timer_task.done():
        mcq_timer_task.cancel()
    
    if tiebreaker_state.get("speed_timer_task") and not tiebreaker_state["speed_timer_task"].done():
        tiebreaker_state["speed_timer_task"].cancel()
    
    if context.user_data.get("paragraph_timer_task") and not context.user_data["paragraph_timer_task"].done():
        context.user_data["paragraph_timer_task"].cancel()

    in_progress = False
    waiting_for_mcq_answer = False
    context.user_data.clear()
    active_players.clear()
    player_scores.clear()
    answered_questions.clear()
    current_turn_index = 0
    
    # Reset states
    review_state = {
        "awaiting_admin_review": False,
        "responding_user_id": None,
        "paragraph_answer": None
    }
    
    tiebreaker_state = {
        "in_progress": False,
        "tied_players": [],
        "current_phase": None,
        "speed_round_question": None,
        "waiting_for_speed_answer": False,
        "first_responder": None,
        "speed_timer_task": None
    }
    
    save_game_state()
    await context.bot.send_message(chat_id=group_chat_id, text="Quiz has been stopped.")

async def skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to skip current turn"""
    global current_turn_index, waiting_for_mcq_answer, mcq_timer_task
    
    if not update.message or not update.effective_user:
        return
        
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Only admins can skip turns.")
        return
    
    if not in_progress:
        await update.message.reply_text("No quiz is currently in progress.")
        return
    
    # Cancel any active timers
    if mcq_timer_task and not mcq_timer_task.done():
        mcq_timer_task.cancel()
        
    if context.user_data.get("paragraph_timer_task") and not context.user_data["paragraph_timer_task"].done():
        context.user_data["paragraph_timer_task"].cancel()
    
    # Reset waiting states
    waiting_for_mcq_answer = False
    context.user_data["waiting_for_paragraph"] = False
    
    # Clear review state if waiting
    if review_state["awaiting_admin_review"]:
        review_state["awaiting_admin_review"] = False
        review_state["responding_user_id"] = None
        review_state["paragraph_answer"] = None
    
    # Get current player name for message
    if current_turn_index < len(active_players):
        user_id = active_players[current_turn_index]
        user = await context.bot.get_chat(user_id)
        await context.bot.send_message(
            chat_id=group_chat_id, 
            text=f"⏭️ Admin skipped {user.first_name}'s turn."
        )
    
    current_turn_index += 1
    save_game_state()
    await next_turn(context)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current game status"""
    if not update.message:
        return
        
    status_lines = ["📊 **Game Status**"]
    
    if not in_progress and not tiebreaker_state["in_progress"]:
        status_lines.append("• No quiz in progress")
        if active_players:
            status_lines.append(f"• {len(active_players)} players waiting to start")
        else:
            status_lines.append("• No players joined")
    elif tiebreaker_state["in_progress"]:
        status_lines.append("🏆 **Tiebreaker in Progress**")
        tied_names = []
        for uid in tiebreaker_state["tied_players"]:
            try:
                user = await context.bot.get_chat(uid)
                tied_names.append(user.first_name)
            except:
                tied_names.append(f"User {uid}")
        status_lines.append(f"• Tied players: {', '.join(tied_names)}")
        status_lines.append(f"• Phase: {tiebreaker_state['current_phase']}")
        if tiebreaker_state["waiting_for_speed_answer"]:
            status_lines.append("• Waiting for speed round answers")
    else:
        status_lines.append("• Quiz in progress")
        status_lines.append(f"• Players: {len(active_players)}")
        status_lines.append(f"• Questions answered: {len(answered_questions)}/{len(question_pool)}")
        
        if current_turn_index < len(active_players):
            current_user_id = active_players[current_turn_index]
            try:
                current_user = await context.bot.get_chat(current_user_id)
                status_lines.append(f"• Current turn: {current_user.first_name}")
            except:
                status_lines.append(f"• Current turn: User {current_user_id}")
        
        if waiting_for_mcq_answer:
            status_lines.append("• Waiting for MCQ answer")
        elif context.user_data.get("waiting_for_paragraph"):
            status_lines.append("• Waiting for paragraph answer")
        elif review_state["awaiting_admin_review"]:
            status_lines.append("• Waiting for admin review")
        else:
            status_lines.append("• Waiting for question selection")
    
    # Show current scores
    if player_scores:
        status_lines.append("\n📈 **Current Scores:**")
        sorted_scores = sorted(player_scores.items(), key=lambda x: x[1], reverse=True)
        for uid, score in sorted_scores:
            try:
                user = await context.bot.get_chat(uid)
                status_lines.append(f"• {user.first_name}: {score}")
            except:
                status_lines.append(f"• User {uid}: {score}")
    
    # Show admin info
    admin_names = []
    for admin_id in ALL_ADMIN_IDS:
        try:
            admin = await context.bot.get_chat(admin_id)
            admin_names.append(admin.first_name)
        except:
            admin_names.append(f"ID:{admin_id}")
    
    status_lines.append(f"\n👑 **Admins:** {', '.join(admin_names)}")
    
    await update.message.reply_text("\n".join(status_lines), parse_mode="Markdown")

def detect_tie():
    """Detect if there's a tie in the current scores"""
    if not player_scores:
        return []
    
    max_score = max(player_scores.values())
    tied_players = [uid for uid, score in player_scores.items() if score == max_score]
    
    return tied_players if len(tied_players) > 1 else []

async def tiebreaker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to start tiebreaker"""
    global tiebreaker_state
    
    if not update.message or not update.effective_user:
        return
        
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Only admins can start tiebreaker.")
        return
    
    if tiebreaker_state["in_progress"]:
        await update.message.reply_text("Tiebreaker already in progress.")
        return
    
    tied_players = detect_tie()
    if not tied_players:
        await update.message.reply_text("No tie detected. Cannot start tiebreaker.")
        return
    
    tiebreaker_state["in_progress"] = True
    tiebreaker_state["tied_players"] = tied_players
    tiebreaker_state["current_phase"] = "speed_round"
    
    save_game_state()
    
    # Get tied player names
    tied_names = []
    for uid in tied_players:
        try:
            user = await context.bot.get_chat(uid)
            tied_names.append(user.first_name)
        except:
            tied_names.append(f"User {uid}")
    
    await context.bot.send_message(
        chat_id=group_chat_id,
        text=f"🏆 **TIEBREAKER ROUND**\n\nTied players: {', '.join(tied_names)}\n\nStarting speed round..."
    )
    
    await start_speed_round(context)

async def start_speed_round(context: ContextTypes.DEFAULT_TYPE):
    """Start the speed round phase of tiebreaker"""
    # Find tiebreaker MCQ questions
    tiebreaker_questions = [q for q in questions_data if q.get("is_tiebreaker") and q["type"] == "mcq"]
    
    if not tiebreaker_questions:
        await context.bot.send_message(
            chat_id=group_chat_id,
            text="No speed round questions available. Moving to paragraph phase..."
        )
        await start_paragraph_tiebreaker(context)
        return
    
    question = random.choice(tiebreaker_questions)
    tiebreaker_state["speed_round_question"] = question
    tiebreaker_state["waiting_for_speed_answer"] = True
    tiebreaker_state["first_responder"] = None
    
    # Format question
    options = question["options"]
    lettered_options = [f"{chr(97 + i)}) {opt}" for i, opt in enumerate(options)]
    question_text = f"⚡ **SPEED ROUND**\n{question['question']}\nOptions:\n" + "\n".join(lettered_options) + "\n\n**First correct answer wins!**"
    
    msg = await context.bot.send_message(chat_id=group_chat_id, text=question_text, parse_mode="Markdown")
    
    # Start 30-second timer
    tiebreaker_state["speed_timer_task"] = asyncio.create_task(
        handle_speed_round_timeout(context, msg.message_id, question_text)
    )
    
    save_game_state()

async def handle_speed_round_timeout(context: ContextTypes.DEFAULT_TYPE, message_id: int, original_text: str):
    """Handle speed round timeout"""
    try:
        await show_timer(context, group_chat_id, message_id, 30, original_text)
        await asyncio.sleep(1)
        
        if tiebreaker_state["waiting_for_speed_answer"]:
            tiebreaker_state["waiting_for_speed_answer"] = False
            await context.bot.send_message(
                chat_id=group_chat_id,
                text="⏰ Speed round time's up! No winner. Moving to paragraph phase..."
            )
            await start_paragraph_tiebreaker(context)
    except asyncio.CancelledError:
        pass

async def start_paragraph_tiebreaker(context: ContextTypes.DEFAULT_TYPE):
    """Start the paragraph phase of tiebreaker"""
    # Find tiebreaker paragraph questions
    tiebreaker_questions = [q for q in questions_data if q.get("is_tiebreaker") and q["type"] == "paragraph"]
    
    if not tiebreaker_questions:
        await declare_shared_winners(context)
        return
    
    question = random.choice(tiebreaker_questions)
    tiebreaker_state["current_phase"] = "paragraph"
    
    # Get tied player names
    tied_names = []
    for uid in tiebreaker_state["tied_players"]:
        try:
            user = await context.bot.get_chat(uid)
            tied_names.append(user.first_name)
        except:
            tied_names.append(f"User {uid}")
    
    question_text = f"📝 **PARAGRAPH TIEBREAKER**\n\n{question['question']}\n\nTied players ({', '.join(tied_names)}), please submit your answers. Admin will judge the best response."
    
    await context.bot.send_message(chat_id=group_chat_id, text=question_text, parse_mode="Markdown")
    save_game_state()

async def declare_shared_winners(context: ContextTypes.DEFAULT_TYPE):
    """Declare shared winners when tiebreaker is exhausted"""
    tied_names = []
    for uid in tiebreaker_state["tied_players"]:
        try:
            user = await context.bot.get_chat(uid)
            tied_names.append(user.first_name)
        except:
            tied_names.append(f"User {uid}")
    
    await context.bot.send_message(
        chat_id=group_chat_id,
        text=f"🏆 **SHARED VICTORY!**\n\nCongratulations to our co-winners: {', '.join(tied_names)}\n\nThe prize will be split among the winners!"
    )
    
    # Reset tiebreaker state
    tiebreaker_state["in_progress"] = False
    save_game_state()

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
    save_game_state()

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global current_turn_index, waiting_for_mcq_answer, mcq_timer_task, review_state, tiebreaker_state

    if not update.message or not update.effective_user:
        return

    # Handle tiebreaker speed round answers
    if tiebreaker_state["waiting_for_speed_answer"] and update.effective_user.id in tiebreaker_state["tied_players"]:
        await handle_speed_round_answer(update, context)
        return

    # Handle tiebreaker paragraph answers
    if tiebreaker_state["in_progress"] and tiebreaker_state["current_phase"] == "paragraph" and update.effective_user.id in tiebreaker_state["tied_players"]:
        await handle_tiebreaker_paragraph(update, context)
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
        save_game_state()
        await next_turn(context)
        return

    # Handle paragraph answers
    if context.user_data.get("waiting_for_paragraph") and update.effective_user.id == context.user_data.get("responding_to"):
        # Cancel the paragraph timer since user answered
        if context.user_data.get("paragraph_timer_task") and not context.user_data["paragraph_timer_task"].done():
            context.user_data["paragraph_timer_task"].cancel()
        
        # Store answer in global review state
        review_state["paragraph_answer"] = update.message.text
        review_state["responding_user_id"] = update.effective_user.id
        review_state["awaiting_admin_review"] = True
        
        context.user_data["waiting_for_paragraph"] = False
        save_game_state()
        
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
        lettered_options = [f"{chr(97 + i)}) {opt}" for i, opt in enumerate(options)]
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
    
    save_game_state()

async def handle_speed_round_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle speed round answer during tiebreaker"""
    if not tiebreaker_state["waiting_for_speed_answer"]:
        return
    
    question = tiebreaker_state["speed_round_question"]
    user = update.effective_user
    user_response = update.message.text.strip()
    
    # Cancel timer
    if tiebreaker_state["speed_timer_task"] and not tiebreaker_state["speed_timer_task"].done():
        tiebreaker_state["speed_timer_task"].cancel()
    
    # Check answer
    correct_answer = question["answer"].strip().lower()
    options = question["options"]
    options_map = {chr(97 + i): opt.strip().lower() for i, opt in enumerate(options)}
    options_map.update({chr(65 + i): opt.strip().lower() for i, opt in enumerate(options)})
    
    normalized_response = user_response.replace(" ", "").lower()
    
    if normalized_response in options_map:
        interpreted_response = options_map[normalized_response]
    else:
        interpreted_response = normalized_response
    
    if interpreted_response == correct_answer:
        # Winner found!
        tiebreaker_state["waiting_for_speed_answer"] = False
        tiebreaker_state["in_progress"] = False
        
        await context.bot.send_message(
            chat_id=group_chat_id,
            text=f"🏆 **TIEBREAKER WINNER!**\n\n{user.first_name} answered correctly first and wins the quiz!"
        )
        save_game_state()
    else:
        await context.bot.send_message(
            chat_id=group_chat_id,
            text=f"❌ {user.first_name}, that's incorrect. Keep trying!"
        )

async def handle_tiebreaker_paragraph(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle paragraph answer during tiebreaker"""
    await context.bot.send_message(
        chat_id=group_chat_id,
        text=f"📝 {update.effective_user.first_name}'s tiebreaker answer received: \"{update.message.text}\"\n\nAdmin can use /approve {update.effective_user.first_name} to declare them the winner, or wait for other answers."
    )

async def handle_paragraph_timeout(context: ContextTypes.DEFAULT_TYPE, message_id: int, original_text: str):
    """Handle paragraph timeout"""
    global current_turn_index
    try:
        await show_timer(context, group_chat_id, message_id, 30, original_text)
        await asyncio.sleep(1)
        
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
            save_game_state()
            await next_turn(context)
    except asyncio.CancelledError:
        pass

async def handle_mcq_timeout(context: ContextTypes.DEFAULT_TYPE, message_id: int, original_text: str):
    """Handle MCQ timeout"""
    global waiting_for_mcq_answer, current_turn_index
    try:
        await show_timer(context, group_chat_id, message_id, 30, original_text)
        await asyncio.sleep(1)
        
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
            save_game_state()
            await next_turn(context)
    except asyncio.CancelledError:
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
        return
        
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
    
    # Check for tie
    tied_players = detect_tie()
    if tied_players:
        tied_names = []
        for uid in tied_players:
            try:
                user = await context.bot.get_chat(uid)
                tied_names.append(user.first_name)
            except:
                tied_names.append(f"User {uid}")
        
        await context.bot.send_message(
            chat_id=group_chat_id,
            text=f"🤝 **TIE DETECTED!**\n\nTied players: {', '.join(tied_names)}\n\nAdmin can use /tiebreaker to start tiebreaker rounds."
        )
    
    save_game_state()

async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global review_state, current_turn_index, tiebreaker_state
    
    if not update.message or not update.effective_user:
        return
    
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Only admins can approve answers.")
        return
    
    # Handle tiebreaker paragraph approval
    if tiebreaker_state["in_progress"] and tiebreaker_state["current_phase"] == "paragraph":
        # Extract player name from command if provided (e.g., "/approve John")
        command_parts = update.message.text.split()
        if len(command_parts) > 1:
            winner_name = " ".join(command_parts[1:])
            # Find player by name
            winner_id = None
            for uid in tiebreaker_state["tied_players"]:
                try:
                    user = await context.bot.get_chat(uid)
                    if user.first_name.lower() == winner_name.lower():
                        winner_id = uid
                        break
                except:
                    continue
            
            if winner_id:
                tiebreaker_state["in_progress"] = False
                user = await context.bot.get_chat(winner_id)
                await context.bot.send_message(
                    chat_id=group_chat_id,
                    text=f"🏆 **TIEBREAKER WINNER!**\n\n{user.first_name} wins the quiz!"
                )
                save_game_state()
                return
            else:
                await update.message.reply_text(f"Player '{winner_name}' not found in tied players.")
                return
        else:
            await update.message.reply_text("Please specify the winner: /approve [player_name]")
            return
    
    # Handle regular paragraph approval
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
    save_game_state()
    await next_turn(context)

async def reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global review_state, current_turn_index
    
    if not update.message or not update.effective_user:
        return
    
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Only admins can reject answers.")
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
    save_game_state()
    await next_turn(context)

if __name__ == "__main__":
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("join", join))
    app.add_handler(CommandHandler("begin", begin))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("skip", skip))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("tiebreaker", tiebreaker))
    app.add_handler(CommandHandler("approve", approve))
    app.add_handler(CommandHandler("reject", reject))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    app.run_polling()