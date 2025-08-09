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

QUESTIONS_FILE = "tkh_quiz2.json"
STATE_FILE = "game_states.pkl"

# Global data structures
questions_data = []
question_pool = {}
game_states = {}  # chat_id -> game_state dictionary

def is_admin(user_id):
    """Check if user is an admin"""
    return user_id in ALL_ADMIN_IDS

def get_game_state(chat_id):
    """Get or create game state for a specific chat"""
    if chat_id not in game_states:
        game_states[chat_id] = {
            "active_players": [],
            "player_scores": {},
            "answered_questions": set(),
            "current_turn_index": 0,
            "in_progress": False,
            "waiting_for_mcq_answer": False,
            "mcq_timer_task": None,
            "game_started": False,  # NEW: Track if /start has been called
            "current_question_player": None,  # NEW: Track which player should answer current question
            "tiebreaker_state": {
                "in_progress": False,
                "tied_players": [],
                "current_phase": None,
                "speed_round_question": None,
                "waiting_for_speed_answer": False,
                "first_responder": None,
                "speed_timer_task": None
            },
            "used_tiebreaker_mcq": set(),   # track already-asked speed-round questions
            "review_state": {
                "awaiting_admin_review": False,
                "responding_user_id": None,
                "paragraph_answer": None
            },
            "user_data": {}  # user_id -> user_specific_data
        }
    return game_states[chat_id]

def get_user_data(chat_id, user_id):
    """Get or create user data for a specific user in a specific chat"""
    game_state = get_game_state(chat_id)
    if user_id not in game_state["user_data"]:
        game_state["user_data"][user_id] = {}
    return game_state["user_data"][user_id]

def save_game_state():
    """Save current game states to file"""
    # Convert sets to lists for JSON serialization
    serializable_states = {}
    for chat_id, state in game_states.items():
        serializable_states[str(chat_id)] = {
            "active_players": state["active_players"],
            "player_scores": state["player_scores"],
            "answered_questions": list(state["answered_questions"]),
            "current_turn_index": state["current_turn_index"],
            "in_progress": state["in_progress"],
            "waiting_for_mcq_answer": state["waiting_for_mcq_answer"],
            "game_started": state["game_started"],
            "current_question_player": state["current_question_player"],
            "tiebreaker_state": {
                "in_progress": state["tiebreaker_state"]["in_progress"],
                "tied_players": state["tiebreaker_state"]["tied_players"],
                "current_phase": state["tiebreaker_state"]["current_phase"],
                "speed_round_question": state["tiebreaker_state"]["speed_round_question"],
                "waiting_for_speed_answer": state["tiebreaker_state"]["waiting_for_speed_answer"],
                "first_responder": state["tiebreaker_state"]["first_responder"]
            },
            "review_state": state["review_state"].copy(),
            "user_data": state["user_data"],
            "timestamp": datetime.now().isoformat()
        }
    
    try:
        with open(STATE_FILE, "wb") as f:
            pickle.dump(serializable_states, f)
    except Exception as e:
        print(f"Error saving game state: {e}")

def load_game_state():
    """Load game states from file"""
    global game_states
    
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "rb") as f:
                serializable_states = pickle.load(f)
            
            game_states = {}
            for chat_id_str, state in serializable_states.items():
                chat_id = int(chat_id_str)
                game_states[chat_id] = {
                    "active_players": state.get("active_players", []),
                    "player_scores": state.get("player_scores", {}),
                    "answered_questions": set(state.get("answered_questions", [])),
                    "current_turn_index": state.get("current_turn_index", 0),
                    "in_progress": state.get("in_progress", False),
                    "waiting_for_mcq_answer": state.get("waiting_for_mcq_answer", False),
                    "mcq_timer_task": None,  # Don't restore timer tasks
                    "game_started": state.get("game_started", False),
                    "current_question_player": state.get("current_question_player", None),
                    "tiebreaker_state": {
                        "in_progress": state.get("tiebreaker_state", {}).get("in_progress", False),
                        "tied_players": state.get("tiebreaker_state", {}).get("tied_players", []),
                        "current_phase": state.get("tiebreaker_state", {}).get("current_phase", None),
                        "speed_round_question": state.get("tiebreaker_state", {}).get("speed_round_question", None),
                        "waiting_for_speed_answer": state.get("tiebreaker_state", {}).get("waiting_for_speed_answer", False),
                        "first_responder": state.get("tiebreaker_state", {}).get("first_responder", None),
                        "speed_timer_task": None  # Don't restore timer tasks
                    },
                    "review_state": state.get("review_state", {
                        "awaiting_admin_review": False,
                        "responding_user_id": None,
                        "paragraph_answer": None
                    }),
                    "user_data": state.get("user_data", {})
                }
            
            print(f"Game states loaded for {len(game_states)} groups")
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
    if not update.message or not update.effective_user:
        return
    
    # Only admins can use /start
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Only admins can start a new quiz session.")
        return
    
    chat_id = update.effective_chat.id
    game_state = get_game_state(chat_id)
    
    # Check if there's already an active session or players waiting
    if (game_state["in_progress"] or 
        game_state["tiebreaker_state"]["in_progress"] or 
        game_state["active_players"]):
        await update.message.reply_text("Cannot start a new quiz. There's already a game in progress or players waiting to start. Use /stop first if you need to reset.")
        return
    
    game_state["game_started"] = True  # Mark that /start has been called
    save_game_state()
    
    await update.message.reply_text("Welcome to the Bible Study Quiz! Type /join to participate. The admin will close entry soon.")

async def join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    
    chat_id = update.effective_chat.id
    game_state = get_game_state(chat_id)
    user = update.effective_user
    
    # Check if /start has been called
    if not game_state["game_started"]:
        await update.message.reply_text("Please wait for the admin to start the quiz with /start first.")
        return
    
    if user.id not in game_state["active_players"] and not game_state["in_progress"]:
        game_state["active_players"].append(user.id)
        game_state["player_scores"][user.id] = 0
        save_game_state()
        await update.message.reply_text(f"{user.first_name} has joined the quiz!")
    elif game_state["in_progress"]:
        await update.message.reply_text("The quiz is already in progress. Wait for the next one.")

async def begin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Only admins can start the quiz.")
        return

    chat_id = update.effective_chat.id
    game_state = get_game_state(chat_id)

    if not game_state["active_players"]:
        await update.message.reply_text("No players have joined.")
        return

    # Rebuild question pool to exclude tiebreaker questions
    if not question_pool:
        await update.message.reply_text("No regular questions available! Please add non-tiebreaker questions to the quiz.")
        return

    game_state["in_progress"] = True
    game_state["current_turn_index"] = 0
    game_state["answered_questions"] = set()
    game_state["current_question_player"] = None
    
    # Reset review state
    game_state["review_state"] = {
        "awaiting_admin_review": False,
        "responding_user_id": None,
        "paragraph_answer": None
    }
    
    # Reset tiebreaker state
    game_state["tiebreaker_state"] = {
        "in_progress": False,
        "tied_players": [],
        "current_phase": None,
        "speed_round_question": None,
        "waiting_for_speed_answer": False,
        "first_responder": None,
        "speed_timer_task": None
    }

    save_game_state()
    await context.bot.send_message(chat_id=chat_id, text="Quiz starting now!")
    await next_turn(context, chat_id)

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
        
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Only admins can stop the quiz.")
        return

    chat_id = update.effective_chat.id
    game_state = get_game_state(chat_id)

    # Cancel any running timers
    if game_state["mcq_timer_task"] and not game_state["mcq_timer_task"].done():
        game_state["mcq_timer_task"].cancel()
    
    if game_state["tiebreaker_state"].get("speed_timer_task") and not game_state["tiebreaker_state"]["speed_timer_task"].done():
        game_state["tiebreaker_state"]["speed_timer_task"].cancel()
    
    # Cancel paragraph timers for all users
    for user_id, user_data in game_state["user_data"].items():
        if user_data.get("paragraph_timer_task") and not user_data["paragraph_timer_task"].done():
            user_data["paragraph_timer_task"].cancel()

    # Reset game state for this chat
    game_state["in_progress"] = False
    game_state["waiting_for_mcq_answer"] = False
    game_state["active_players"].clear()
    game_state["player_scores"].clear()
    game_state["answered_questions"].clear()
    game_state["current_turn_index"] = 0
    game_state["user_data"].clear()
    game_state["game_started"] = False
    game_state["current_question_player"] = None
    
    # Reset states
    game_state["review_state"] = {
        "awaiting_admin_review": False,
        "responding_user_id": None,
        "paragraph_answer": None
    }
    
    game_state["tiebreaker_state"] = {
        "in_progress": False,
        "tied_players": [],
        "current_phase": None,
        "speed_round_question": None,
        "waiting_for_speed_answer": False,
        "first_responder": None,
        "speed_timer_task": None
    }
    
    save_game_state()
    await context.bot.send_message(chat_id=chat_id, text="Quiz has been stopped.")

async def skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command to skip current turn"""
    if not update.message or not update.effective_user:
        return
        
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Only admins can skip turns.")
        return
    
    chat_id = update.effective_chat.id
    game_state = get_game_state(chat_id)
    
    if not game_state["in_progress"]:
        await update.message.reply_text("No quiz is currently in progress.")
        return
    
    # Cancel any active timers
    if game_state["mcq_timer_task"] and not game_state["mcq_timer_task"].done():
        game_state["mcq_timer_task"].cancel()
        
    # Cancel paragraph timer for current user
    if game_state["current_turn_index"] < len(game_state["active_players"]):
        current_user_id = game_state["active_players"][game_state["current_turn_index"]]
        user_data = get_user_data(chat_id, current_user_id)
        if user_data.get("paragraph_timer_task") and not user_data["paragraph_timer_task"].done():
            user_data["paragraph_timer_task"].cancel()
    
    # Reset waiting states
    game_state["waiting_for_mcq_answer"] = False
    game_state["current_question_player"] = None
    
    # Clear user waiting states
    for user_data in game_state["user_data"].values():
        user_data["waiting_for_paragraph"] = False
    
    # Clear review state if waiting
    if game_state["review_state"]["awaiting_admin_review"]:
        game_state["review_state"]["awaiting_admin_review"] = False
        game_state["review_state"]["responding_user_id"] = None
        game_state["review_state"]["paragraph_answer"] = None
    
    # Get current player name for message
    if game_state["current_turn_index"] < len(game_state["active_players"]):
        user_id = game_state["active_players"][game_state["current_turn_index"]]
        user = await context.bot.get_chat(user_id)
        await context.bot.send_message(
            chat_id=chat_id, 
            text=f"⏭️ Admin skipped {user.first_name}'s turn."
        )
    
    game_state["current_turn_index"] += 1
    save_game_state()
    await next_turn(context, chat_id)

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current game status - ADMIN ONLY"""
    if not update.message or not update.effective_user:
        return
    
    # Only admins can use /status
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Only admins can check game status.")
        return
    
    chat_id = update.effective_chat.id
    game_state = get_game_state(chat_id)
    
    status_lines = ["📊 **Game Status**"]
    
    if not game_state["in_progress"] and not game_state["tiebreaker_state"]["in_progress"]:
        status_lines.append("• No quiz in progress")
        if game_state["active_players"]:
            status_lines.append(f"• {len(game_state['active_players'])} players waiting to start")
        else:
            status_lines.append("• No players joined")
    elif game_state["tiebreaker_state"]["in_progress"]:
        status_lines.append("🏆 **Tiebreaker in Progress**")
        tied_names = []
        for uid in game_state["tiebreaker_state"]["tied_players"]:
            try:
                user = await context.bot.get_chat(uid)
                tied_names.append(user.first_name)
            except:
                tied_names.append(f"User {uid}")
        status_lines.append(f"• Tied players: {', '.join(tied_names)}")
        status_lines.append(f"• Phase: {game_state['tiebreaker_state']['current_phase']}")
        if game_state["tiebreaker_state"]["waiting_for_speed_answer"]:
            status_lines.append("• Waiting for speed round answers")
    else:
        status_lines.append("• Quiz in progress")
        status_lines.append(f"• Players: {len(game_state['active_players'])}")
        status_lines.append(f"• Questions answered: {len(game_state['answered_questions'])}/{len(question_pool)}")
        
        if game_state["current_turn_index"] < len(game_state["active_players"]):
            current_user_id = game_state["active_players"][game_state["current_turn_index"]]
            try:
                current_user = await context.bot.get_chat(current_user_id)
                status_lines.append(f"• Current turn: {current_user.first_name}")
            except:
                status_lines.append(f"• Current turn: User {current_user_id}")
        
        if game_state["waiting_for_mcq_answer"]:
            status_lines.append("• Waiting for MCQ answer")
        elif any(user_data.get("waiting_for_paragraph") for user_data in game_state["user_data"].values()):
            status_lines.append("• Waiting for paragraph answer")
        elif game_state["review_state"]["awaiting_admin_review"]:
            status_lines.append("• Waiting for admin review")
        else:
            status_lines.append("• Waiting for question selection")
    
    # Show current scores
    if game_state["player_scores"]:
        status_lines.append("\n📈 **Current Scores:**")
        sorted_scores = sorted(game_state["player_scores"].items(), key=lambda x: x[1], reverse=True)
        for uid, score in sorted_scores:
            try:
                user = await context.bot.get_chat(uid)
                status_lines.append(f"• {user.first_name}: {score}")
            except:
                status_lines.append(f"• User {uid}: {score}")
    
    # Show admin info - get names instead of IDs
    admin_names = []
    for admin_id in ALL_ADMIN_IDS:
        try:
            admin = await context.bot.get_chat(admin_id)
            # Use username if available, otherwise first_name
            if hasattr(admin, 'username') and admin.username:
                admin_names.append(f"@{admin.username}")
            else:
                admin_names.append(admin.first_name)
        except:
            # If we can't get the name, use a generic label (not the ID)
            admin_names.append("Admin")
    
    status_lines.append(f"\n👑 **Admins:** {', '.join(admin_names)}")
    
    await update.message.reply_text("\n".join(status_lines), parse_mode="Markdown")

def detect_tie(chat_id):
    """Detect if there's a tie in the current scores"""
    game_state = get_game_state(chat_id)
    if not game_state["player_scores"]:
        return []
    
    max_score = max(game_state["player_scores"].values())
    tied_players = [uid for uid, score in game_state["player_scores"].items() if score == max_score]
    
    return tied_players if len(tied_players) > 1 else []

async def tiebreaker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    """Admin command to start tiebreaker"""
    if not update.message or not update.effective_user:
        return
        
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Only admins can start tiebreaker.")
        return
    
    chat_id = update.effective_chat.id
    game_state = get_game_state(chat_id)
    
    if game_state["tiebreaker_state"]["in_progress"]:
        await update.message.reply_text("Tiebreaker already in progress.")
        return
    
    tied_players = detect_tie(chat_id)
    if not tied_players:
        await update.message.reply_text("No tie detected. Cannot start tiebreaker.")
        return
    
    game_state["used_tiebreaker_mcq"].clear() 
    game_state["tiebreaker_state"]["in_progress"] = True
    game_state["tiebreaker_state"]["tied_players"] = tied_players
    game_state["tiebreaker_state"]["current_phase"] = "speed_round"
    
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
        chat_id=chat_id,
        text=f"🏆 **TIEBREAKER ROUND**\n\nTied players: {', '.join(tied_names)}\n\nStarting speed round..."
    )
    
    await start_speed_round(context, chat_id)

async def start_speed_round(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Pick an *unused* tie-breaker MCQ; if none left → paragraph phase."""
    game_state = get_game_state(chat_id)

    # 1. Collect *unused* MCQ questions
    available = [
        q for q in questions_data
        if q.get("is_tiebreaker") and q["type"] == "mcq" and json.dumps(q) not in game_state["used_tiebreaker_mcq"]
    ]

    if not available:                       # <-- no more speed questions
        await context.bot.send_message(
            chat_id=chat_id,
            text="All speed-round questions exhausted. Moving to paragraph phase…"
        )
        await start_paragraph_tiebreaker(context, chat_id)
        return

    # 2. Pick & mark as used
    question = random.choice(available)
    game_state["used_tiebreaker_mcq"].add(json.dumps(question))  # JSON string is hashable
    game_state["tiebreaker_state"]["speed_round_question"] = question
    game_state["tiebreaker_state"]["waiting_for_speed_answer"] = True
    game_state["tiebreaker_state"]["first_responder"] = None

    # 3. Send to group
    options = question["options"]
    lettered_options = [f"{chr(97 + i)}) {opt}" for i, opt in enumerate(options)]
    txt = (f"⚡ **SPEED ROUND** ({len(available)-1} left)\n"
           f"{question['question']}\n" +
           "\n".join(lettered_options) +
           "\n\n**First correct answer wins!**")
    msg = await context.bot.send_message(chat_id=chat_id, text=txt, parse_mode="Markdown")

    # 4. Start 30-s timer
    game_state["tiebreaker_state"]["speed_timer_task"] = asyncio.create_task(
        handle_speed_round_timeout(context, chat_id, msg.message_id, txt)
    )
    save_game_state()

async def handle_speed_round_timeout(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, original_text: str):
    """Handle speed round timeout"""
    game_state = get_game_state(chat_id)
    try:
        await show_timer(context, chat_id, message_id, 30, original_text)
        await asyncio.sleep(1)
        
        if game_state["tiebreaker_state"]["waiting_for_speed_answer"]:
            # ⏰ Time's up, next speed question
            game_state["tiebreaker_state"]["waiting_for_speed_answer"] = False
            await context.bot.send_message(
                chat_id=chat_id,
                text="⏰ Time's up! Next speed-round question..."
            )
            await start_speed_round(context, chat_id)
    except asyncio.CancelledError:
        pass

async def start_paragraph_tiebreaker(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Start the paragraph phase of tiebreaker"""
    game_state = get_game_state(chat_id)
    
    # Find tiebreaker paragraph questions
    tiebreaker_questions = [q for q in questions_data if q.get("is_tiebreaker") and q["type"] == "paragraph"]
    
    if not tiebreaker_questions:
        await declare_shared_winners(context, chat_id)
        return
    
    question = random.choice(tiebreaker_questions)
    game_state["tiebreaker_state"]["current_phase"] = "paragraph"
    
    # Get tied player names
    tied_names = []
    for uid in game_state["tiebreaker_state"]["tied_players"]:
        try:
            user = await context.bot.get_chat(uid)
            tied_names.append(user.first_name)
        except:
            tied_names.append(f"User {uid}")
    
    question_text = f"📝 **PARAGRAPH TIEBREAKER**\n\n{question['question']}\n\nTied players ({', '.join(tied_names)}), please submit your answers. Admin will judge the best response."
    
    await context.bot.send_message(chat_id=chat_id, text=question_text, parse_mode="Markdown")
    save_game_state()

async def declare_shared_winners(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Declare shared winners when tiebreaker is exhausted"""
    game_state = get_game_state(chat_id)
    
    tied_names = []
    for uid in game_state["tiebreaker_state"]["tied_players"]:
        try:
            user = await context.bot.get_chat(uid)
            tied_names.append(user.first_name)
        except:
            tied_names.append(f"User {uid}")
    
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🏆 **SHARED VICTORY!**\n\nCongratulations to our co-winners: {', '.join(tied_names)}\n\nThe prize will be split among the winners!"
    )
    
    # Reset tiebreaker state
    game_state["tiebreaker_state"]["in_progress"] = False
    save_game_state()

async def next_turn(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game_state = get_game_state(chat_id)

    if game_state["current_turn_index"] >= len(game_state["active_players"]):
        # Show leaderboard after each complete round
        await show_leaderboard(context, chat_id, is_final=False)
        
        # Check if we should end the quiz or continue
        if len(game_state["answered_questions"]) >= len(question_pool):
            await end_quiz(context, chat_id)
            return
        
        # Reset for next round
        game_state["current_turn_index"] = 0

    user_id = game_state["active_players"][game_state["current_turn_index"]]
    user = await context.bot.get_chat(user_id)
    available = [k for k in question_pool if k not in game_state["answered_questions"]]

    if len(available) < len(game_state["active_players"]) - game_state["current_turn_index"]:
        await context.bot.send_message(
            chat_id=chat_id,
            text="Not enough questions left for every remaining player. Quiz ends now!"
        )
        await end_quiz(context, chat_id)
        return

    if not available:
        await end_quiz(context, chat_id)
        return

    mention = f"[{user.first_name}](tg://user?id={user.id})"
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"Your turn, {mention}! Pick a number from {available}",
        parse_mode="Markdown"
    )
    save_game_state()

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return

    chat_id = update.effective_chat.id
    game_state = get_game_state(chat_id)
    user_data = get_user_data(chat_id, update.effective_user.id)

    # Handle tiebreaker speed round answers
    if game_state["tiebreaker_state"]["waiting_for_speed_answer"] and update.effective_user.id in game_state["tiebreaker_state"]["tied_players"]:
        await handle_speed_round_answer(update, context)
        return

    # Handle tiebreaker paragraph answers
    if game_state["tiebreaker_state"]["in_progress"] and game_state["tiebreaker_state"]["current_phase"] == "paragraph" and update.effective_user.id in game_state["tiebreaker_state"]["tied_players"]:
        await handle_tiebreaker_paragraph(update, context)
        return

    if not game_state["in_progress"]:
        return

    # Handle MCQ answers - ONLY from the player whose turn it is AND who is expected to answer
    if (game_state["waiting_for_mcq_answer"] and 
        game_state["current_question_player"] and 
        update.effective_user.id == game_state["current_question_player"]):
        
        # Cancel the timer since user answered
        if game_state["mcq_timer_task"] and not game_state["mcq_timer_task"].done():
            game_state["mcq_timer_task"].cancel()
        
        await check_mcq_answer(update, context)
        game_state["waiting_for_mcq_answer"] = False
        game_state["current_question_player"] = None
        game_state["current_turn_index"] += 1
        save_game_state()
        await next_turn(context, chat_id)
        return

    # Handle paragraph answers - ONLY from the player whose turn it is AND who is expected to answer
    if (user_data.get("waiting_for_paragraph") and 
        game_state["current_question_player"] and 
        update.effective_user.id == game_state["current_question_player"]):
        
        # Cancel the paragraph timer since user answered
        if user_data.get("paragraph_timer_task") and not user_data["paragraph_timer_task"].done():
            user_data["paragraph_timer_task"].cancel()
        
        # Store answer in review state
        game_state["review_state"]["paragraph_answer"] = update.message.text
        game_state["review_state"]["responding_user_id"] = update.effective_user.id
        game_state["review_state"]["awaiting_admin_review"] = True
        
        user_data["waiting_for_paragraph"] = False
        save_game_state()
        
        # Send to admin for review
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Admin, please review {update.effective_user.first_name}'s answer: \"{update.message.text}\"\n\nReply with /approve or /reject."
        )
        return

    # Handle additional responses from users who already answered paragraph questions
    if (game_state["review_state"]["awaiting_admin_review"] and 
        update.effective_user.id == game_state["review_state"]["responding_user_id"] and
        update.effective_user.id in game_state["active_players"]):  # Only if still active
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"{update.effective_user.first_name}, only your first response is considered. Please wait for admin review."
        )
        return

    # Ignore messages from users who are not active players
    if update.effective_user.id not in game_state["active_players"]:
        return

    # Handle wrong user trying to answer MCQ
    if game_state["waiting_for_mcq_answer"] and update.effective_user.id != game_state["current_question_player"]:
        # Don't respond to prevent confusion - ignore the message
        return

    # Handle wrong user trying to answer paragraph question
    if any(user_data.get("waiting_for_paragraph") for user_data in game_state["user_data"].values()) and update.effective_user.id != game_state["current_question_player"]:
        # Don't respond to prevent confusion - ignore the message
        return

    # Handle question selection (only if it's the user's turn and we're not waiting for an answer)
    if (game_state["current_turn_index"] >= len(game_state["active_players"]) or 
        update.effective_user.id != game_state["active_players"][game_state["current_turn_index"]] or 
        game_state["waiting_for_mcq_answer"] or
        game_state["current_question_player"]):
        return

    chosen = update.message.text.strip()
    if chosen not in question_pool or chosen in game_state["answered_questions"]:
        await context.bot.send_message(chat_id=chat_id, text="Invalid or already used number. Try again.")
        return

    question = question_pool[chosen]
    game_state["answered_questions"].add(chosen)
    game_state["current_question_player"] = update.effective_user.id  # Set who should answer this question

    if question["type"] == "mcq":
        options = question["options"]
        lettered_options = [f"{chr(97 + i)}) {opt}" for i, opt in enumerate(options)]
        question_text = f"{question['question']}\nOptions:\n" + "\n".join(lettered_options)
        msg = await context.bot.send_message(chat_id=chat_id, text=question_text)
        
        # Set up answer checking data for the CURRENT player only
        user_data["current_answer"] = question["answer"].strip().lower()
        user_data["options_map"] = {
            chr(97 + i): opt.strip().lower() for i, opt in enumerate(options)
        }
        # Also include uppercase letters for user input flexibility
        user_data["options_map"].update({
            chr(65 + i): opt.strip().lower() for i, opt in enumerate(options)
        })
        
        # Set state to wait for MCQ answer
        game_state["waiting_for_mcq_answer"] = True
        
        # Start timer
        game_state["mcq_timer_task"] = asyncio.create_task(handle_mcq_timeout(context, chat_id, msg.message_id, question_text))

    elif question["type"] == "paragraph":
        question_text = f"{question['question']} (You have 30 seconds to respond.)"
        msg = await context.bot.send_message(chat_id=chat_id, text=question_text)
        
        # Set up paragraph answer waiting for the CURRENT player only
        user_data["waiting_for_paragraph"] = True
        
        # Start paragraph timer
        user_data["paragraph_timer_task"] = asyncio.create_task(
            handle_paragraph_timeout(context, chat_id, msg.message_id, question_text)
        )
    
    save_game_state()

async def handle_speed_round_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle speed-round answer: keep timer running until correct."""
    chat_id = update.effective_chat.id
    game_state = get_game_state(chat_id)

    if not game_state["tiebreaker_state"]["waiting_for_speed_answer"]:
        return

    question = game_state["tiebreaker_state"]["speed_round_question"]
    user = update.effective_user
    user_response = update.message.text.strip()

    # Normalise the response exactly like before
    options = question["options"]
    options_map = {chr(97 + i): opt.strip().lower() for i, opt in enumerate(options)}
    options_map.update({chr(65 + i): opt.strip().lower() for i, opt in enumerate(options)})

    normalised = user_response.replace(" ", "").lower()
    chosen = options_map.get(normalised, normalised)
    correct = question["answer"].strip().lower()

    if chosen == correct:
        # ✅ First correct answer → stop everything
        if game_state["tiebreaker_state"]["speed_timer_task"] and not game_state["tiebreaker_state"]["speed_timer_task"].done():
            game_state["tiebreaker_state"]["speed_timer_task"].cancel()

        game_state["tiebreaker_state"]["waiting_for_speed_answer"] = False
        game_state["tiebreaker_state"]["in_progress"] = False

        await context.bot.send_message(
            chat_id=chat_id,
            text=f"⚡ **{user.first_name}** got it first and wins the speed round!"
        )
        save_game_state()
        return

    # ❌ Wrong answer → timer continues, nothing else happens
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"❌ {user.first_name}, that's wrong – keep trying!"
    )

async def handle_tiebreaker_paragraph(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle paragraph answer during tiebreaker"""
    chat_id = update.effective_chat.id
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"📝 {update.effective_user.first_name}'s tiebreaker answer received: \"{update.message.text}\"\n\nAdmin can use /approve {update.effective_user.first_name} to declare them the winner, or wait for other answers."
    )

async def handle_paragraph_timeout(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, original_text: str):
    """Handle paragraph timeout"""
    game_state = get_game_state(chat_id)
    try:
        await show_timer(context, chat_id, message_id, 30, original_text)
        await asyncio.sleep(1)
        
        # Find the user who was supposed to answer using current_question_player
        if game_state["current_question_player"]:
            user = await context.bot.get_chat(game_state["current_question_player"])
            await context.bot.send_message(
                chat_id=chat_id, 
                text=f"⏰ Time's up, {user.first_name}! Moving to next turn."
            )
            
            # Clear waiting state for this user
            user_data = get_user_data(chat_id, game_state["current_question_player"])
            user_data["waiting_for_paragraph"] = False
        
        game_state["current_question_player"] = None
        game_state["current_turn_index"] += 1
        save_game_state()
        await next_turn(context, chat_id)
    except asyncio.CancelledError:
        pass

async def handle_mcq_timeout(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, original_text: str):
    """Handle MCQ timeout"""
    game_state = get_game_state(chat_id)
    try:
        await show_timer(context, chat_id, message_id, 30, original_text)
        await asyncio.sleep(1)
        
        if game_state["waiting_for_mcq_answer"]:
            game_state["waiting_for_mcq_answer"] = False
            
            # Find the user who was supposed to answer using current_question_player
            if game_state["current_question_player"]:
                user = await context.bot.get_chat(game_state["current_question_player"])
                user_data = get_user_data(chat_id, game_state["current_question_player"])
                correct_answer = user_data.get("current_answer", "")
                await context.bot.send_message(
                    chat_id=chat_id, 
                    text=f"⏰ Time's up, {user.first_name}! The correct answer was: {correct_answer}"
                )
            
            game_state["current_question_player"] = None
            game_state["current_turn_index"] += 1
            save_game_state()
            await next_turn(context, chat_id)
    except asyncio.CancelledError:
        pass

async def check_mcq_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    game_state = get_game_state(chat_id)
    user_data = get_user_data(chat_id, update.effective_user.id)
    
    user = update.effective_user
    user_id = user.id
    correct_answer = user_data.get("current_answer", "").strip().lower()
    options_map = user_data.get("options_map", {})
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
        game_state["player_scores"][user_id] += 1
        await context.bot.send_message(chat_id=chat_id, text=f"✅ {user.first_name}, that's correct!")
    else:
        await context.bot.send_message(chat_id=chat_id, text=f"❌ {user.first_name}, that's incorrect. The correct answer was: {correct_answer}")

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

async def show_leaderboard(context: ContextTypes.DEFAULT_TYPE, chat_id: int, is_final=False):
    """Show current leaderboard"""
    game_state = get_game_state(chat_id)
    
    leaderboard = sorted(game_state["player_scores"].items(), key=lambda x: x[1], reverse=True)
    title = "🏆 Final Leaderboard:" if is_final else "📊 Current Leaderboard:"
    result = [title]
    for i, (uid, score) in enumerate(leaderboard):
        name = (await context.bot.get_chat(uid)).first_name
        result.append(f"{i+1}. {name} — {score} point(s)")
    await context.bot.send_message(chat_id=chat_id, text="\n".join(result))

async def end_quiz(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    game_state = get_game_state(chat_id)
    game_state["in_progress"] = False
    await show_leaderboard(context, chat_id, is_final=True)
    
    # Check for tie
    tied_players = detect_tie(chat_id)
    if tied_players:
        tied_names = []
        for uid in tied_players:
            try:
                user = await context.bot.get_chat(uid)
                tied_names.append(user.first_name)
            except:
                tied_names.append(f"User {uid}")
        
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"🤝 **TIE DETECTED!**\n\nTied players: {', '.join(tied_names)}\n\nAdmin can use /tiebreaker to start tiebreaker rounds."
        )
    
    save_game_state()

async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Only admins can approve answers.")
        return
    
    chat_id = update.effective_chat.id
    game_state = get_game_state(chat_id)
    
    # Handle tiebreaker paragraph approval
    if game_state["tiebreaker_state"]["in_progress"] and game_state["tiebreaker_state"]["current_phase"] == "paragraph":
        # Extract player name from command if provided (e.g., "/approve John")
        command_parts = update.message.text.split()
        if len(command_parts) > 1:
            winner_name = " ".join(command_parts[1:])
            # Find player by name
            winner_id = None
            for uid in game_state["tiebreaker_state"]["tied_players"]:
                try:
                    user = await context.bot.get_chat(uid)
                    if user.first_name.lower() == winner_name.lower():
                        winner_id = uid
                        break
                except:
                    continue
            
            if winner_id:
                game_state["tiebreaker_state"]["in_progress"] = False
                user = await context.bot.get_chat(winner_id)
                await context.bot.send_message(
                    chat_id=chat_id,
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
    if not game_state["review_state"]["awaiting_admin_review"]:
        await update.message.reply_text("No answer is currently awaiting review.")
        return
        
    user_id = game_state["review_state"]["responding_user_id"]
    if not user_id:
        await update.message.reply_text("Error: No user found for this review.")
        return
        
    game_state["player_scores"][user_id] += 1
    user = await context.bot.get_chat(user_id)
    await context.bot.send_message(chat_id=chat_id, text=f"✅ {user.first_name}'s answer has been approved.")
    
    # Clear review state
    game_state["review_state"] = {
        "awaiting_admin_review": False,
        "responding_user_id": None,
        "paragraph_answer": None
    }
    
    # Move to next turn
    game_state["current_question_player"] = None
    game_state["current_turn_index"] += 1
    save_game_state()
    await next_turn(context, chat_id)

async def reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.effective_user:
        return
    
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Only admins can reject answers.")
        return
    
    chat_id = update.effective_chat.id
    game_state = get_game_state(chat_id)
    
    if not game_state["review_state"]["awaiting_admin_review"]:
        await update.message.reply_text("No answer is currently awaiting review.")
        return
        
    user_id = game_state["review_state"]["responding_user_id"]
    if not user_id:
        await update.message.reply_text("Error: No user found for this review.")
        return
        
    user = await context.bot.get_chat(user_id)
    await context.bot.send_message(chat_id=chat_id, text=f"❌ {user.first_name}'s answer has been rejected.")
    
    # Clear review state
    game_state["review_state"] = {
        "awaiting_admin_review": False,
        "responding_user_id": None,
        "paragraph_answer": None
    }
    
    # Move to next turn
    game_state["current_question_player"] = None
    game_state["current_turn_index"] += 1
    save_game_state()
    await next_turn(context, chat_id)

async def remove_player(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/remove <first_name|@username> — admin only"""
    if not update.message or not update.effective_user:
        return
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Only admins can remove players.")
        return

    chat_id = update.effective_chat.id
    game_state = get_game_state(chat_id)
    if not game_state["active_players"]:
        await update.message.reply_text("No players in the game.")
        return

    # Extract the name/username to remove
    if not context.args:
        await update.message.reply_text("Usage: /remove <first_name|@username>")
        return
    target = " ".join(context.args).lstrip("@").lower()

    # Find matching user
    victim_id = None
    victim_index = None
    for i, uid in enumerate(game_state["active_players"]):
        try:
            user = await context.bot.get_chat(uid)
            # compare first name OR username
            if user.first_name.lower() == target or (user.username and user.username.lower() == target):
                victim_id = uid
                victim_index = i
                break
        except Exception:
            continue

    if victim_id is None:
        await update.message.reply_text(f"Player '{target}' not found.")
        return

    # Check if this player was currently answering a question
    was_current_player = (game_state["current_question_player"] == victim_id)
    was_awaiting_review = (game_state["review_state"]["awaiting_admin_review"] and 
                          game_state["review_state"]["responding_user_id"] == victim_id)

    # ---- actual removal ----
    game_state["active_players"].remove(victim_id)
    game_state["player_scores"].pop(victim_id, None)
    game_state["user_data"].pop(victim_id, None)

    # If the removed player was the current question player, clear it
    if game_state["current_question_player"] == victim_id:
        game_state["current_question_player"] = None
        game_state["waiting_for_mcq_answer"] = False
        # Cancel any active timers
        if game_state["mcq_timer_task"] and not game_state["mcq_timer_task"].done():
            game_state["mcq_timer_task"].cancel()

    # Clear review state if this player was awaiting review
    if was_awaiting_review:
        game_state["review_state"] = {
            "awaiting_admin_review": False,
            "responding_user_id": None,
            "paragraph_answer": None
        }

    # Adjust current_turn_index if necessary
    if victim_index < game_state["current_turn_index"]:
        game_state["current_turn_index"] -= 1
    elif victim_index == game_state["current_turn_index"]:
        # If we removed the player whose turn it currently is, don't increment
        pass
    
    # Ensure current_turn_index doesn't go out of bounds
    if game_state["current_turn_index"] >= len(game_state["active_players"]) and game_state["active_players"]:
        game_state["current_turn_index"] = 0
    
    save_game_state()

    user = await context.bot.get_chat(victim_id)
    await context.bot.send_message(
        chat_id=chat_id,
        text=f"🚫 {user.first_name} has been removed from the quiz."
    )

    # If the removed player was currently answering or awaiting review, move to next turn
    if (game_state["in_progress"] and (was_current_player or was_awaiting_review)):
        game_state["current_turn_index"] += 1
        await next_turn(context, chat_id)
    # If game in progress and no one is currently answering a question, move to next turn
    elif (game_state["in_progress"] and 
          not game_state["waiting_for_mcq_answer"] and 
          not game_state["current_question_player"] and
          not any(user_data.get("waiting_for_paragraph") for user_data in game_state["user_data"].values()) and
          not game_state["review_state"]["awaiting_admin_review"]):
        await next_turn(context, chat_id)

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
    app.add_handler(CommandHandler("remove", remove_player))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_message))
    app.run_polling()