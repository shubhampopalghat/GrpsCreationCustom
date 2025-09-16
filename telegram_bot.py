import logging
import json
import os
import asyncio
import threading
import queue
import time
import zipfile
import tempfile
import shutil
from typing import Optional
from telegram import Update, Message, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, filters, CallbackQueryHandler
)
from telegram.constants import ParseMode

from BigBotFinal import run_group_creation_process, API_ID, API_HASH, get_account_summary, send_account_stats_and_cleanup
from telethon.sync import TelegramClient
from telethon.errors import SessionPasswordNeededError

# --- Configuration ---
CONFIG_FILE = 'bot_config.json'
SESSIONS_DIR = 'sessions'

# Loading sticker ID
LOADING_STICKER_ID = "CAACAgUAAxkBAAEPUtFovPZ08EglcUMRAg0mpuQjV8eXRAACtRkAAiEb2VXfF6Me-ipGBjYE"

# Channel verification settings
REQUIRED_CHANNEL = "@NexoUnion"  # Replace with your channel username
CHANNEL_LINK = "https://t.me/NexoUnion"  # Replace with your channel link

# --- FIXED SETTINGS ---
FIXED_DELAY = 20  # Reduced from 2 minutes to 20 seconds
FIXED_MESSAGES_PER_GROUP = 10
FIXED_MESSAGES = [
    "Hello", "Ladies",
    "And", "Gentle",
    "Man", "This",
    "This", "Is",
    "My", "World"
]

# States for conversation
(LOGIN_METHOD_CHOICE, GET_PHONE, GET_LOGIN_CODE, GET_2FA_PASS, UPLOAD_ZIP, GET_GROUP_COUNT) = range(6)
ACTIVE_PROCESSES = {}
CANCELLATION_REQUESTED = {}  # Track when user requests cancellation
CANCELLATION_REQUESTED = {}  # Track when user requests cancellation

# Channel verification tracking (no longer used - bot is admin-only)
VERIFIED_USERS = set()  # Track users who have verified channel membership

# --- Helper Functions ---
async def show_loading(update: Update, text: str = "⏳ Processing...") -> Optional[Message]:
    """Show loading sticker and message"""
    try:
        if update.message:
            return await update.message.reply_sticker(LOADING_STICKER_ID)
        elif update.callback_query:
            return await update.callback_query.message.reply_sticker(LOADING_STICKER_ID)
    except Exception as e:
        logging.error(f"Failed to send loading sticker: {e}")
        # Fallback to text message
        if update.message:
            return await update.message.reply_text(text)
        elif update.callback_query:
            return await update.callback_query.message.reply_text(text)
    return None

async def hide_loading(loading_msg: Optional[Message]):
    """Delete loading message/sticker"""
    try:
        if loading_msg:
            await loading_msg.delete()
    except Exception as e:
        logging.error(f"Failed to delete loading message: {e}")

def load_config():
    if not os.path.exists(SESSIONS_DIR): 
        os.makedirs(SESSIONS_DIR)
        print(f"Created sessions directory: {SESSIONS_DIR}")
    
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'w') as f:
            json.dump({"BOT_TOKEN": "YOUR_BOT_TOKEN_HERE", "OWNER_IDS": [], "ADMIN_IDS": []}, f, indent=4)
        print("CONFIG CREATED: Please edit 'bot_config.json' with your bot token and owner IDs.")
        exit()
    
    with open(CONFIG_FILE, 'r') as f: 
        return json.load(f)

def backup_session(session_path: str, user_id: int):
    """Create a backup of the session file"""
    try:
        session_file = f"{session_path}.session"
        if os.path.exists(session_file):
            backup_dir = os.path.join(SESSIONS_DIR, str(user_id), "backups")
            os.makedirs(backup_dir, exist_ok=True)
            
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            backup_name = f"{os.path.basename(session_path)}_{timestamp}.session"
            backup_path = os.path.join(backup_dir, backup_name)
            
            shutil.copy2(session_file, backup_path)
            print(f"Session backed up: {backup_path}")
            return backup_path
    except Exception as e:
        print(f"Failed to backup session: {e}")
    return None

def save_config(config_data):
    with open(CONFIG_FILE, 'w') as f: json.dump(config_data, f, indent=4)

config = load_config()
# Handle backward compatibility - convert old OWNER_ID to OWNER_IDS array
if 'OWNER_ID' in config and 'OWNER_IDS' not in config:
    config['OWNER_IDS'] = [config['OWNER_ID']] if config['OWNER_ID'] != 0 else []
    del config['OWNER_ID']
    save_config(config)
    print("Converted OWNER_ID to OWNER_IDS array for backward compatibility")

OWNER_IDS, ADMIN_IDS = config['OWNER_IDS'], config['ADMIN_IDS']

async def check_channel_membership(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if user is a member of the required channel"""
    try:
        # Remove @ symbol if present for the API call
        channel_username = REQUIRED_CHANNEL.replace('@', '')
        
        # Get chat member status
        chat_member = await context.bot.get_chat_member(chat_id=f"@{channel_username}", user_id=user_id)
        
        # Check if user is a member (member, administrator, or creator)
        return chat_member.status in ['member', 'administrator', 'creator']
    except Exception as e:
        print(f"Error checking channel membership for user {user_id}: {e}")
        return False

async def send_channel_verification_message(update: Update, context: ContextTypes.DEFAULT_TYPE, message_type="reply"):
    """Send channel verification message with join link and verify button - styled like Nexo Union"""
    verification_text = (
        f"🔔 **Join our channel to access the bot!**\n\n"
        f"📢 **Channel:** {REQUIRED_CHANNEL}\n"
        f"🔗 **Link:** {CHANNEL_LINK}\n\n"
        f"💡 **You must join the channel to use bot features**"
    )
    
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Channel", url=CHANNEL_LINK)],
        [InlineKeyboardButton("✅ Verify", callback_data="verify_channel")]
    ])
    
    if message_type == "reply" and update.message:
        await update.message.reply_text(verification_text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    elif message_type == "edit" and hasattr(update, 'callback_query'):
        await update.callback_query.edit_message_text(verification_text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
    else:
        # Fallback for other cases
        if update.message:
            await update.message.reply_text(verification_text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)

def authorized(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        
        # Only owner and admins can access the bot
        if user_id in OWNER_IDS or user_id in ADMIN_IDS:
            return await func(update, context, *args, **kwargs)
        
        # Regular users are not allowed - show access denied message
        await update.message.reply_text(
            "⛔ **Access Denied!**\n\n"
            "This bot is restricted to authorized users only.\n"
            "Contact the bot owner for access.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    return wrapper

def admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id in ADMIN_IDS:
            return await func(update, context, *args, **kwargs)
        else: 
            await update.message.reply_text("⛔ **Admin Access Required!**\n\nOnly admins can access account management.", parse_mode=ParseMode.MARKDOWN)
    return wrapper

def get_main_keyboard():
    """Create main menu keyboard"""
    keyboard = [
        [InlineKeyboardButton("🔐 Login Your Accounts", callback_data="start_creation")],
        [InlineKeyboardButton("🚀 Start Groups Creation", callback_data="view_accounts")],
        [InlineKeyboardButton("📊 Bot Statistics", callback_data="bot_stats")],
        [InlineKeyboardButton("👨‍💻 Developer Info", callback_data="developer_info")]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_admin_keyboard():
    """Create admin management keyboard for owner"""
    keyboard = [
        [InlineKeyboardButton("➕ Add Admin", callback_data="add_admin_prompt")],
        [InlineKeyboardButton("➖ Remove Admin", callback_data="remove_admin_prompt")],
        [InlineKeyboardButton("📋 List Admins", callback_data="list_admins")],
        [InlineKeyboardButton("🔙 Back to Main", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def validate_session(session_path, session_name, user_id=None):
    """Validate if a session file is still working with timeout protection"""
    client = None
    try:
        # Check if session file exists and has content
        session_file = f"{session_path}.session"
        if not os.path.exists(session_file):
            print(f"Session file not found: {session_file} for user {user_id}")
            return {'valid': False, 'reason': 'File not found'}
        
        file_size = os.path.getsize(session_file)
        if file_size == 0:
            print(f"Session file is empty: {session_file} for user {user_id}")
            return {'valid': False, 'reason': 'File empty'}
        
        print(f"Validating session: {session_path} (size: {file_size} bytes) for user {user_id}")
        
        # Use the API credentials from BigBotFinal with timeout protection
        client = TelegramClient(session_path, API_ID, API_HASH)
        
        # Set connection timeout to 15 seconds
        try:
            await asyncio.wait_for(client.connect(), timeout=15.0)
        except asyncio.TimeoutError:
            print(f"Connection timeout for session: {session_path} for user {user_id}")
            return {'valid': False, 'reason': 'Connection timeout'}
        
        # Check authorization with timeout
        try:
            is_authorized = await asyncio.wait_for(client.is_user_authorized(), timeout=10.0)
        except asyncio.TimeoutError:
            print(f"Authorization check timeout for session: {session_path} for user {user_id}")
            await client.disconnect()
            return {'valid': False, 'reason': 'Authorization timeout'}
        
        if is_authorized:
            try:
                me = await asyncio.wait_for(client.get_me(), timeout=10.0)
                if me:
                    print(f"Session valid for: {me.first_name} (@{me.username}) - User ID: {user_id}")
                    result = {
                        'valid': True,
                        'name': f"{me.first_name or ''} {me.last_name or ''}".strip() or 'Unknown',
                        'username': me.username or 'N/A',
                        'id': me.id,
                        'phone': session_name,
                        'user_id': user_id
                    }
                    await client.disconnect()
                    return result
                else:
                    print(f"Failed to get user details for session: {session_path}")
                    await client.disconnect()
                    return {'valid': False, 'reason': 'No user details'}
            except asyncio.TimeoutError:
                print(f"Get user details timeout for session: {session_path} for user {user_id}")
                await client.disconnect()
                return {'valid': False, 'reason': 'User details timeout'}
        else:
            print(f"Session not authorized: {session_path} for user {user_id}")
            await client.disconnect()
            return {'valid': False, 'reason': 'Not authorized'}
            
    except Exception as e:
        print(f"Error validating session {session_path} for user {user_id}: {e}")
        if client:
            try:
                await asyncio.wait_for(client.disconnect(), timeout=5.0)
            except:
                pass
        return {'valid': False, 'reason': str(e)}

def get_account_keyboard(sessions):
    """Create keyboard for account selection"""
    keyboard = []
    for i, session in enumerate(sessions[:10]):  # Limit to 10 accounts per page
        keyboard.append([InlineKeyboardButton(f"📱 {session}", callback_data=f"account_{session}")])
    keyboard.append([InlineKeyboardButton("🔙 Back to Main", callback_data="main_menu")])
    return InlineKeyboardMarkup(keyboard)

def ensure_user_session_path(user_id: int, session_name: str) -> str:
    """Ensure proper session path construction for user-specific sessions"""
    user_session_dir = os.path.join(SESSIONS_DIR, str(user_id))
    os.makedirs(user_session_dir, exist_ok=True)
    session_path = os.path.join(user_session_dir, session_name)
    print(f"Session path for user {user_id}: {session_path}")
    return session_path

def get_session_file_path(user_id: int, session_name: str) -> str:
    """Get the full path to the .session file"""
    user_session_dir = os.path.join(SESSIONS_DIR, str(user_id))
    session_file_path = os.path.join(user_session_dir, f"{session_name}.session")
    print(f"Session file path for user {user_id}: {session_file_path}")
    return session_file_path

def escape_markdown(text: str) -> str:
    """Escape special characters that can break Markdown formatting"""
    if not text:
        return text
    
    # Characters that need escaping in Markdown
    special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    
    escaped_text = str(text)
    for char in special_chars:
        escaped_text = escaped_text.replace(char, f'\\{char}')
    
    return escaped_text

def debug_session_storage(user_id: int):
    """Debug function to show session storage structure for a user"""
    user_session_dir = os.path.join(SESSIONS_DIR, str(user_id))
    print(f"=== Session Storage Debug for User {user_id} ===")
    print(f"User session directory: {user_session_dir}")
    
    if os.path.exists(user_session_dir):
        session_files = [f for f in os.listdir(user_session_dir) if f.endswith('.session')]
        print(f"Found {len(session_files)} session files:")
        for session_file in session_files:
            session_path = os.path.join(user_session_dir, session_file)
            file_size = os.path.getsize(session_path) if os.path.exists(session_path) else 0
            print(f"  - {session_file} (size: {file_size} bytes)")
            
        # Check backup directory
        backup_dir = os.path.join(user_session_dir, "backups")
        if os.path.exists(backup_dir):
            backup_files = [f for f in os.listdir(backup_dir) if f.endswith('.session')]
            print(f"Found {len(backup_files)} backup files in backups/")
    else:
        print("User session directory does not exist")
    print("=" * 50)

async def process_zip_accounts(update: Update, context: ContextTypes.DEFAULT_TYPE, zip_file_path: str):
    """Process ZIP file containing session and JSON files"""
    user_id = update.effective_user.id
    user_session_dir = os.path.join(SESSIONS_DIR, str(user_id))
    os.makedirs(user_session_dir, exist_ok=True)
    
    temp_dir = tempfile.mkdtemp()
    accounts_info = []
    
    try:
        with zipfile.ZipFile(zip_file_path, 'r') as zip_ref:
            zip_ref.extractall(temp_dir)
        
        # Find JSON files and corresponding session files (limit to 20)
        json_files = [f for f in os.listdir(temp_dir) if f.endswith('.json')][:20]
        
        if len([f for f in os.listdir(temp_dir) if f.endswith('.json')]) > 20:
            await update.message.reply_text(
                "⚠️ **Account Limit Reached!**\n\n"
                f"📊 **Found:** {len([f for f in os.listdir(temp_dir) if f.endswith('.json')])} accounts in ZIP\n"
                f"🔒 **Limit:** 20 accounts maximum\n\n"
                f"✅ **Processing first 20 accounts only**",
                parse_mode=ParseMode.MARKDOWN
            )
        
        total_accounts = len(json_files)
        processed_count = 0
        
        # Show initial progress
        await update.message.reply_text(
            f"📁 **Processing ZIP Accounts**\n\n"
            f"📊 **Progress:** 0/{total_accounts} accounts processed\n"
            f"{'▱' * 10} 0%",
            parse_mode=ParseMode.MARKDOWN
        )
        
        for json_file in json_files:
            processed_count += 1
            
            # Update progress bar
            progress_percentage = int((processed_count / total_accounts) * 100)
            filled_bars = int((processed_count / total_accounts) * 10)
            empty_bars = 10 - filled_bars
            progress_bar = '▰' * filled_bars + '▱' * empty_bars
            
            await update.message.reply_text(
                f"📁 **Processing ZIP Accounts**\n\n"
                f"📊 **Progress:** {processed_count}/{total_accounts} accounts processed\n"
                f"{progress_bar} {progress_percentage}%\n\n"
                f"🔄 **Currently processing:** {json_file.replace('.json', '')}",
                parse_mode=ParseMode.MARKDOWN
            )
            phone_number = json_file.replace('.json', '')
            session_file = f"{phone_number}.session"
            
            if session_file in os.listdir(temp_dir):
                # Load account data from JSON
                with open(os.path.join(temp_dir, json_file), 'r') as f:
                    account_data = json.load(f)
                
                # Copy session file to user's directory
                source_session = os.path.join(temp_dir, session_file)
                dest_session = os.path.join(user_session_dir, session_file)
                shutil.copy2(source_session, dest_session)
                
                # Test the account and get details
                try:
                    session_path = os.path.join(user_session_dir, phone_number)
                    api_id = account_data.get('app_id', API_ID)
                    api_hash = account_data.get('app_hash', API_HASH)
                    twofa = account_data.get('twoFA', '')
                    
                    client = TelegramClient(session_path, api_id, api_hash)
                    await client.connect()
                    
                    if not await client.is_user_authorized():
                        if twofa:
                            try:
                                await client.sign_in(password=twofa)
                            except Exception as auth_error:
                                print(f"2FA failed for {phone_number}: {auth_error}")
                                await client.disconnect()
                                continue
                        else:
                            print(f"Account {phone_number} not authorized and no 2FA provided")
                            await client.disconnect()
                            continue
                    
                    me = await client.get_me()
                    await client.disconnect()
                    
                    account_info = {
                        'session_path': session_path,
                        'phone': account_data.get('phone', phone_number),
                        'api_id': api_id,
                        'api_hash': api_hash,
                        'user_details': {
                            'name': f"{me.first_name} {me.last_name or ''}".strip(),
                            'username': me.username or 'N/A',
                            'id': me.id
                        }
                    }
                    accounts_info.append(account_info)
                    
                except Exception as e:
                    await update.message.reply_text(f"❌ **Failed to process {phone_number}:** {str(e)}", parse_mode=ParseMode.MARKDOWN)
        
        # Show final completion message
        await update.message.reply_text(
            f"✅ **ZIP Processing Complete!**\n\n"
            f"📊 **Results:** {len(accounts_info)}/{total_accounts} accounts successfully processed\n"
            f"{'▰' * 10} 100%",
            parse_mode=ParseMode.MARKDOWN
        )
        
        if accounts_info:
            # Send success report
            report_text = f"🎉 **Account Loading Summary**\n\n📊 **Successfully Loaded:** {len(accounts_info)} accounts\n\n"
            
            for i, acc in enumerate(accounts_info[:10], 1):  # Show first 10
                details = acc['user_details']
                # Escape special characters for safe display
                safe_name = escape_markdown(details['name'])
                safe_username = escape_markdown(details['username'])
                safe_phone = escape_markdown(acc['phone'])
                
                report_text += f"📱 `{i}.` {safe_name} (@{safe_username}) - {safe_phone}\n"
            
            if len(accounts_info) > 10:
                report_text += f"\n... and {len(accounts_info) - 10} more accounts"
            
            await update.message.reply_text(report_text, parse_mode=ParseMode.MARKDOWN)
            
            # Store accounts info for group creation
            context.user_data['zip_accounts'] = accounts_info
            await update.message.reply_text("🔢 **How many groups per account?**\n\n💡 *Same number will run on each loaded account, simultaneously*")
            context.user_data['conversation_state'] = GET_GROUP_COUNT
        else:
            await update.message.reply_text("❌ **No Valid Accounts Found**\n\nPlease check your ZIP file format and try again.", parse_mode=ParseMode.MARKDOWN)
            context.user_data['conversation_state'] = None
            
    except Exception as e:
        await update.message.reply_text(f"❌ **ZIP Processing Error:** {str(e)}", parse_mode=ParseMode.MARKDOWN)
        context.user_data['conversation_state'] = None
    finally:
        # Clean up temp directory
        shutil.rmtree(temp_dir, ignore_errors=True)
        if os.path.exists(zip_file_path):
            os.remove(zip_file_path)

async def send_login_success_details(update: Update, context: ContextTypes.DEFAULT_TYPE, session_path: str, phone: str):
    """Connects to a session, sends details, and then disconnects."""
    try:
        client = TelegramClient(session_path, API_ID, API_HASH)
        await client.connect()
        
        if not await client.is_user_authorized():
            await client.disconnect()
            await update.message.reply_text("❌ **Session Invalid**\n\nPlease try logging in again.", parse_mode=ParseMode.MARKDOWN)
            context.user_data['conversation_state'] = None
            return
        
        me = await client.get_me()
        if not me:
            await client.disconnect()
            await update.message.reply_text("❌ **Failed to get user details**\n\nPlease try again.", parse_mode=ParseMode.MARKDOWN)
            context.user_data['conversation_state'] = None
            return
            
        # Escape special characters for safe Markdown display
        safe_first_name = escape_markdown(me.first_name or '')
        safe_last_name = escape_markdown(me.last_name or '')
        safe_username = escape_markdown(me.username or 'N/A')
        safe_phone = escape_markdown(phone)
        
        details_text = (
            f"✅ **Account Successfully Logged In!**\n\n"
            f"👤 **Name:** {safe_first_name} {safe_last_name}\n"
            f"🔖 **Username:** @{safe_username}\n"
            f"🆔 **ID:** `{me.id}`\n"
            f"📱 **Phone:** `{safe_phone}`\n\n"
            f"🔐 **Session Status:** Active & Saved\n\n"
            f"⚠️ **Important:** Wait 2-3 minutes before starting group creation to avoid account freezing!"
        )
        await update.message.reply_text(details_text, parse_mode=ParseMode.MARKDOWN)
        
        # Send session file
        session_file = f"{session_path}.session"
        if os.path.exists(session_file):
            try:
                with open(session_file, 'rb') as file:
                    await context.bot.send_document(
                        chat_id=update.effective_chat.id,
                        document=file,
                        caption="📁 **Session File**\n\nKeep this file safe! It contains your login session.",
                        parse_mode=ParseMode.MARKDOWN
                    )
            except Exception as e:
                print(f"Failed to send session file: {e}")
        
        # Properly disconnect and save session
        await client.disconnect()
        
        # Verify session file exists and is valid
        if os.path.exists(session_file):
            file_size = os.path.getsize(session_file)
            if file_size > 0:
                print(f"Session file saved successfully: {session_file} ({file_size} bytes)")
                
                # Create backup of the session
                user_id = update.effective_user.id
                backup_path = backup_session(session_path, user_id)
                if backup_path:
                    print(f"Session backup created: {backup_path}")
                
            else:
                print(f"Warning: Session file is empty: {session_file}")
        
        context.user_data['account_info'] = {'session_path': session_path, 'phone': phone}
        
        # Show completion message with instructions
        await update.message.reply_text(
            "🎉 **Login Successful!**\n\n"
            "✅ **Account logged in and session saved successfully!**\n\n"
            "🚀 **Next Steps:**\n"
            "1️⃣ Use `/start` command\n"
            "2️⃣ Click **\"🔐Login your accounts\"** button\n"
            "3️⃣ Follow the setup process\n\n"
            "💡 **Want to login more accounts?**\n"
            "Use `/start` and repeat the same process!\n\n"
            "🔐 **Session Status:** Saved and ready to use",
            parse_mode=ParseMode.MARKDOWN
        )
        
        # Clear conversation state to end the flow
        context.user_data['conversation_state'] = None
        return
        
    except Exception as e:
        print(f"Error in send_login_success_details: {e}")
        await update.message.reply_text(f"❌ **Error getting account details:** {str(e)}", parse_mode=ParseMode.MARKDOWN)
        context.user_data['conversation_state'] = None

# --- Bot Command Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = update.effective_user
    
    # Show loading sticker for 1.5 seconds
    try:
        loading_msg = await update.message.reply_sticker(LOADING_STICKER_ID)
        await asyncio.sleep(1.5)
        await loading_msg.delete()
    except Exception as e:
        logging.error(f"Failed to show loading sticker: {e}")
    
    # Only owner and admins can access the bot
    if user_id in OWNER_IDS or user_id in ADMIN_IDS:
        # Show main menu for authorized users
        welcome_text = (
            f"🤖 **Welcome, {user.first_name}!**\n\n"
            f"🎯 **Group Creation Bot** is ready to serve you!\n\n"
            f"👤 **Your Role:** {'🔑 Owner' if user_id in OWNER_IDS else '👨‍💼 Admin'}\n"
            f"📊 **Status:** ✅ Authorized\n\n"
            f"🚀 **Ready to create groups and manage accounts!**"
        )
        
        # Create keyboard buttons list
        keyboard_buttons = [
            [InlineKeyboardButton("🔐 Login your accounts", callback_data="start_creation")],
            [InlineKeyboardButton("🚀 Start Group Creation", callback_data="view_accounts")],
            [InlineKeyboardButton("📊 Bot Statistics", callback_data="bot_stats")],
            [InlineKeyboardButton("👨‍💻 Developer Info", callback_data="developer_info")]
        ]
        
        if user_id in OWNER_IDS:
            keyboard_buttons.append([InlineKeyboardButton("⚙️ Admin Management", callback_data="admin_menu")])
        
        keyboard = InlineKeyboardMarkup(keyboard_buttons)
        await update.message.reply_text(welcome_text, reply_markup=keyboard, parse_mode=ParseMode.MARKDOWN)
        return
    
    # Regular users are not allowed - show access denied message
    await update.message.reply_text(
        "⛔ **Access Denied!**\n\n"
        "This bot is restricted to authorized users only.\n"
        "Contact the bot owner for access.",
        parse_mode=ParseMode.MARKDOWN
    )
    return

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if query.data == "main_menu":
        # Only owner and admins can access the bot
        if user_id not in OWNER_IDS and user_id not in ADMIN_IDS:
            await query.edit_message_text(
                "⛔ **Access Denied!**\n\n"
                "This bot is restricted to authorized users only.\n"
                "Contact the bot owner for access.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        # Create keyboard buttons list
        keyboard_buttons = [
            [InlineKeyboardButton("🔐 Login your accounts", callback_data="start_creation")],
            [InlineKeyboardButton("🚀 Start Group Creation", callback_data="view_accounts")],
            [InlineKeyboardButton("📊 Bot Statistics", callback_data="bot_stats")],
            [InlineKeyboardButton("👨‍💻 Developer Info", callback_data="developer_info")]
        ]
        
        if user_id in OWNER_IDS:
            keyboard_buttons.append([InlineKeyboardButton("⚙️ Admin Management", callback_data="admin_menu")])
        
        keyboard = InlineKeyboardMarkup(keyboard_buttons)
        await query.edit_message_text(
            "🏠 **Main Menu**\n\nSelect an option below:",
            reply_markup=keyboard,
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif query.data == "start_creation":
        # Only owner and admins can access the bot
        if user_id not in OWNER_IDS and user_id not in ADMIN_IDS:
            await query.edit_message_text(
                "⛔ **Access Denied!**\n\n"
                "This bot is restricted to authorized users only.\n"
                "Contact the bot owner for access.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        if ACTIVE_PROCESSES.get(user_id):
            await query.edit_message_text(
                "⚠️ **Process Already Running!**\n\nYou already have a group creation process active.\n\n🛑 **Use the cancel button below to stop it**",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🛑 Cancel Running Process", callback_data="cancel_process")],
                    [InlineKeyboardButton("🔙 Back to Main", callback_data="main_menu")]
                ])
            )
            return
        
        # Check for existing accounts first
        user_session_dir = os.path.join(SESSIONS_DIR, str(user_id))
        existing_sessions = []
        if os.path.exists(user_session_dir):
            existing_sessions = [s.replace('.session', '') for s in os.listdir(user_session_dir) if s.endswith('.session')]
        
        if existing_sessions:
            account_keyboard = [
                [InlineKeyboardButton("➕ Add New Account", callback_data="add_new_account")],
            ]
            
            await query.edit_message_text(
                f"🔐 **Account Selection**\n\n"
                f"📱 **Found {len(existing_sessions)} existing accounts**\n\n"
                f"Choose to use existing accounts or add new ones:",
                reply_markup=InlineKeyboardMarkup(account_keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            login_keyboard = [
                [InlineKeyboardButton("📱 Manual Login", callback_data="manual_login")],
                [InlineKeyboardButton("📁 ZIP File Login", callback_data="zip_login")],
                [InlineKeyboardButton("🔙 Back to Main", callback_data="main_menu")]
            ]
            
            await query.edit_message_text(
                "🔐 **Choose Login Method**\n\n"
                "📱 **Manual Login:** Enter phone number and complete OTP verification\n\n"
                "📁 **ZIP File Login:** Upload a ZIP file containing session files and account JSON files\n\n"
                "💡 **ZIP Format Expected:**\n"
                "```\n"
                "accounts.zip\n"
                "├── 14944888484.json\n"
                "├── 14944888484.session\n"
                "├── 44858938484.json\n"
                "└── 44858938484.session\n"
                "```",
                reply_markup=InlineKeyboardMarkup(login_keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
        
        context.user_data.clear()
        context.user_data['conversation_state'] = LOGIN_METHOD_CHOICE
    
    elif query.data == "view_accounts":
        # Only owner and admins can access the bot
        if user_id not in OWNER_IDS and user_id not in ADMIN_IDS:
            await query.edit_message_text(
                "⛔ **Access Denied!**\n\n"
                "This bot is restricted to authorized users only.\n"
                "Contact the bot owner for access.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        user_session_dir = os.path.join(SESSIONS_DIR, str(user_id))
        
        if not os.path.exists(user_session_dir):
            await query.edit_message_text(
                "📭 **No Accounts Found**\n\nYou don't have any logged-in accounts.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Main", callback_data="main_menu")]])
            )
            return
        
        # Get session files and validate them (limit to 20)
        session_files = [f for f in os.listdir(user_session_dir) if f.endswith('.session')][:20]
        valid_accounts = []
        total_accounts = len(session_files)
        
        # Debug session storage
        debug_session_storage(user_id)
        
        # Show initial progress message
        await query.edit_message_text(
            f"🔍 **Checking Account Status**\n\n"
            f"📊 **Progress:** 0/{total_accounts} accounts checked\n"
            f"{'▱' * 10} 0%",
            parse_mode=ParseMode.MARKDOWN
        )
        
        for i, session_file in enumerate(session_files, 1):
            session_name = session_file.replace('.session', '')
            session_path = ensure_user_session_path(user_id, session_name)
            
            # Update progress bar
            progress_percentage = int((i / total_accounts) * 100)
            filled_bars = int((i / total_accounts) * 10)
            empty_bars = 10 - filled_bars
            progress_bar = '▰' * filled_bars + '▱' * empty_bars
            
            try:
                await query.edit_message_text(
                    f"🔍 **Checking Account Status**\n\n"
                    f"📊 **Progress:** {i}/{total_accounts} accounts checked\n"
                    f"{progress_bar} {progress_percentage}%\n\n"
                    f"🔄 **Currently checking:** {session_name}",
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception as e:
                print(f"Failed to update progress message: {e}")
            
            # Validate session with overall timeout protection
            try:
                account_info = await asyncio.wait_for(
                    validate_session(session_path, session_name, user_id), 
                    timeout=45.0  # Maximum 45 seconds per session
                )
                if account_info['valid']:
                    valid_accounts.append(account_info)
                else:
                    # Remove invalid session files
                    try:
                        os.remove(os.path.join(user_session_dir, session_file))
                        print(f"Removed invalid session file: {session_file} for user {user_id}")
                    except Exception as e:
                        print(f"Failed to remove invalid session {session_file}: {e}")
            except asyncio.TimeoutError:
                print(f"Session validation timeout (45s) for {session_name} - user {user_id}")
                # Mark as invalid and remove the stuck session
                try:
                    os.remove(os.path.join(user_session_dir, session_file))
                    print(f"Removed timeout session file: {session_file} for user {user_id}")
                except Exception as e:
                    print(f"Failed to remove timeout session {session_file}: {e}")
            except Exception as e:
                print(f"Unexpected error validating session {session_name}: {e}")
        
        # Show completion message
        await query.edit_message_text(
            f"✅ **Account Check Complete!**\n\n"
            f"📊 **Results:** {len(valid_accounts)}/{total_accounts} accounts valid\n"
            f"{'▰' * 10} 100%",
            parse_mode=ParseMode.MARKDOWN
        )
        
        # Wait a moment to show completion
        await asyncio.sleep(1)
        
        if not valid_accounts:
            await query.edit_message_text(
                "📭 **No Valid Accounts Found**\n\nAll sessions have expired or are invalid.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Main", callback_data="main_menu")]])
            )
            return
        
        # Cache available accounts to avoid re-validating after clicking "Use These Accounts"
        account_details_cache = []
        for acc in valid_accounts:
            session_name = acc['phone']
            session_path = ensure_user_session_path(user_id, session_name)
            account_details_cache.append({
                'session_name': session_name,
                'session_path': session_path,
                'phone': session_name,
                'name': acc['name'],
                'username': acc['username']
            })
        context.user_data['available_accounts'] = account_details_cache
        context.user_data['selected_accounts'] = []

        accounts_text = f"👥 **Your Logged Accounts** ({len(valid_accounts)})\n\n"
        for i, account in enumerate(valid_accounts[:15], 1):  # Limit display
            # Escape special characters that can break Markdown
            safe_name = escape_markdown(account['name'])
            safe_username = escape_markdown(account['username'])
            safe_phone = escape_markdown(account['phone'])
            
            accounts_text += f"📱 `{i}.` {safe_name} (@{safe_username}) - {safe_phone}\n"
        
        if len(valid_accounts) > 15:
            accounts_text += f"\n... and {len(valid_accounts) - 15} more accounts"
        
        # Add selection keyboard for group creation
        keyboard = [[InlineKeyboardButton("🚀 Use These Accounts", callback_data="select_from_existing")]]
        keyboard.append([InlineKeyboardButton("🔙 Back to Main", callback_data="main_menu")])
        
        await query.edit_message_text(
            accounts_text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif query.data == "bot_stats":
        # Only owner and admins can access the bot
        if user_id not in OWNER_IDS and user_id not in ADMIN_IDS:
            await query.edit_message_text(
                "⛔ **Access Denied!**\n\n"
                "This bot is restricted to authorized users only.\n"
                "Contact the bot owner for access.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        total_admins = len(ADMIN_IDS)
        total_sessions = 0
        
        for admin_id in ADMIN_IDS:
            admin_session_dir = os.path.join(SESSIONS_DIR, str(admin_id))
            if os.path.exists(admin_session_dir):
                total_sessions += len([f for f in os.listdir(admin_session_dir) if f.endswith('.session')])
        
        stats_text = (
            f"📊 **Bot Statistics**\n\n"
            f"👨‍💼 **Total Admins:** {total_admins}\n"
            f"📱 **Logged Accounts:** {total_sessions}\n"
            f"⚙️ **Messages per Group:** {FIXED_MESSAGES_PER_GROUP}\n"
            f"⏱️ **Fixed Delay:** {FIXED_DELAY} seconds\n"
            f"🔄 **Active Processes:** {len([p for p in ACTIVE_PROCESSES.values() if p])}\n\n"
            f"🤖 **Bot Version:** 2.0 Enhanced"
        )
        
        await query.edit_message_text(
            stats_text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Main", callback_data="main_menu")]]),
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif query.data == "help_menu":
        # Only owner and admins can access the bot
        if user_id not in OWNER_IDS and user_id not in ADMIN_IDS:
            await query.edit_message_text(
                "⛔ **Access Denied!**\n\n"
                "This bot is restricted to authorized users only.\n"
                "Contact the bot owner for access.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        help_text = (
            "ℹ️ **Help & Features**\n\n"
            "🔐 **Login Your Accounts**\n"
            "   • Login with phone number\n"
            "   • Automatic OTP handling\n"
            "   • Session file provided\n\n"
            "🚀 **Star groups cretion** (Admins only)\n"
            "   • See all logged accounts\n"
            "   • Account details and status\n\n"
            "   • Takes groups count and then starts creating groups\n\n"
            "📊 **Statistics**\n"
            "   • Bot usage statistics\n"
            "   • Admin and account counts\n\n"
            "⚙️ **Admin Management** (Owner only)\n"
            "   • Add/remove admins\n"
            "   • List current admins\n\n"
            "🔐 **Security Features**\n"
            "   • Role-based access control\n"
            "   • Secure session storage\n"
            "   • Admin-only account access"
        )
        
        help_keyboard = [
            [InlineKeyboardButton("🔙 Back to Main", callback_data="main_menu")]
        ]
        
        await query.edit_message_text(
            help_text,
            reply_markup=InlineKeyboardMarkup(help_keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif query.data == "developer_info":
        # Only owner and admins can access the bot
        if user_id not in OWNER_IDS and user_id not in ADMIN_IDS:
            await query.edit_message_text(
                "⛔ **Access Denied!**\n\n"
                "This bot is restricted to authorized users only.\n"
                "Contact the bot owner for access.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        developer_text = (
            "👨‍💻 **Developer Information**\n\n"
            "🧑‍💼 **Name:** Mikey\n"
            "👤 **Username:** @OldGcHub\n"
            "📢 **Channel:** @NexoUnion\n"
            "💬 **Group:** @NexoDiscussion\n\n"
            "🛠️ **Tech Stack Used:**\n"
            "• 🐍 **Python** - Core programming language\n"
            "• 🤖 **python-telegram-bot** - Telegram Bot API wrapper\n"
            "• 📡 **Telethon** - Telegram client library\n"
            "• 📁 **JSON** - Configuration and data storage\n"
            "• 🔄 **Asyncio** - Asynchronous programming\n"
            "• 🗂️ **OS/File System** - Session and file management\n"
            "• 📦 **Threading** - Multi-threading support\n"
            "• 🗜️ **ZipFile** - Archive handling\n\n"
            "💡 **About This Bot:**\n"
            "• Advanced Telegram group creation automation\n"
            "• Multi-account session management\n"
            "• Secure authentication system\n"
            "• Role-based access control\n"
            "• Real-time process monitoring\n\n"
            "📞 **Contact Developer:**\n"
            "• Personal: @OldGcHub\n"
            "• Updates: @NexoUnion\n"
            "• Support: @NexoDiscussion"
        )
        
        developer_keyboard = [
            [InlineKeyboardButton("👤 Contact Developer", url="https://t.me/OldGcHub")],
            [InlineKeyboardButton("📢 Join Channel", url="https://t.me/NexoUnion"), 
             InlineKeyboardButton("💬 Join Group", url="https://t.me/NexoDiscussion")],
            [InlineKeyboardButton("ℹ️ Help & Features", callback_data="help_menu")]
        ]
        
        await query.edit_message_text(
            developer_text,
            reply_markup=InlineKeyboardMarkup(developer_keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif query.data == "admin_menu":
        if user_id not in OWNER_IDS:
            await query.edit_message_text(
                "⛔ **Owner Access Required!**\n\nOnly the bot owner can manage admins/n. You can ask @OldGcHub for access of this bot",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Main", callback_data="main_menu")]])
            )
            return
        
        await query.edit_message_text(
            "⚙️ **Admin Management**\n\nSelect an action:",
            reply_markup=get_admin_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif query.data == "add_admin_prompt":
        await query.edit_message_text(
            "➕ **Add New Admin**\n\n"
            "Please send the user ID of the person you want to add as admin.\n\n"
            "💡 **Tip:** Use /start command and check the user ID from their profile",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin Menu", callback_data="admin_menu")]]),
            parse_mode=ParseMode.MARKDOWN
        )
        context.user_data['awaiting_admin_id'] = 'add'
    
    elif query.data == "remove_admin_prompt":
        if not ADMIN_IDS:
            await query.edit_message_text(
                "📭 **No Admins Found**\n\nThere are no admins to remove.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin Menu", callback_data="admin_menu")]])
            )
            return
        
        await query.edit_message_text(
            "➖ **Remove Admin**\n\n"
            "Please send the user ID of the admin you want to remove.\n\n"
            f"📋 **Current Admins:** {', '.join(map(str, ADMIN_IDS))}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin Menu", callback_data="admin_menu")]]),
            parse_mode=ParseMode.MARKDOWN
        )
        context.user_data['awaiting_admin_id'] = 'remove'
    
    elif query.data == "list_admins":
        if not ADMIN_IDS:
            text = "📭 **No Admins Configured**\n\nThere are currently no admins."
        else:
            text = "👨‍💼 **Current Admins**\n\n"
            for i, admin_id in enumerate(ADMIN_IDS, 1):
                text += f"🔹 `{i}.` User ID: `{admin_id}`\n"
        
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Admin Menu", callback_data="admin_menu")]]),
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif query.data == "manual_login":
        # Check current account count limit
        user_session_dir = os.path.join(SESSIONS_DIR, str(user_id))
        existing_accounts = 0
        if os.path.exists(user_session_dir):
            existing_accounts = len([f for f in os.listdir(user_session_dir) if f.endswith('.session')])
        
        if existing_accounts >= 20:
            await query.edit_message_text(
                "🔒 **Account Limit Reached!**\n\n"
                f"📊 **Current Accounts:** {existing_accounts}/20\n"
                f"🚫 **Cannot add more accounts**\n\n"
                f"💡 **Tip:** Remove some accounts first or use existing ones",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="start_creation")]]),
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        await query.edit_message_text(
            "📱 **Manual Account Login**\n\n"
            f"📊 **Current Accounts:** {existing_accounts}/20\n"
            f"➕ **Adding:** Account #{existing_accounts + 1}\n\n"
            "Please send the phone number of the account you want to use.\n\n"
            "📝 **Format:** +15551234567\n"
            "🔐 **Security:** Your session will be saved securely",
            parse_mode=ParseMode.MARKDOWN
        )
        context.user_data['conversation_state'] = GET_PHONE
    
   
    
    elif query.data == "select_from_existing":
        # Use cached accounts from the previous validation step to avoid running validation twice
        available_accounts = context.user_data.get('available_accounts', [])
        if not available_accounts:
            await query.answer("No cached accounts. Please choose 'Use Existing Accounts' first.", show_alert=True)
            return
        
        # Initialize selected list if missing
        context.user_data['selected_accounts'] = context.user_data.get('selected_accounts', [])
        
        keyboard = []
        for i, acc in enumerate(available_accounts):
            is_selected = any(sel['session_path'] == acc['session_path'] for sel in context.user_data['selected_accounts'])
            status = "✅" if is_selected else "⭕"
            safe_name = escape_markdown(acc['name'])
            safe_username = escape_markdown(acc['username'])
            safe_phone = escape_markdown(acc['phone'])
            keyboard.append([InlineKeyboardButton(
                f"{status} {safe_name} (@{safe_username}) - {safe_phone}",
                callback_data=f"toggle_account_{i}"
            )])
        keyboard.append([InlineKeyboardButton("✅ Select All", callback_data="select_all_accounts")])
        keyboard.append([InlineKeyboardButton("❌ Clear All", callback_data="clear_all_accounts")])
        if context.user_data['selected_accounts']:
            keyboard.append([InlineKeyboardButton("🚀 Continue with Selected", callback_data="continue_with_selected")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="view_accounts")])
        
        selected_count = len(context.user_data['selected_accounts'])
        await query.edit_message_text(
            f"📱 **Multi-Select Accounts**\n\n"
            f"**Selected:** {selected_count}/{len(available_accounts)} accounts\n\n"
            f"✅ = Selected, ⭕ = Not Selected\n"
            f"Click accounts to toggle selection.\n\n"
            f"💡 Select the accounts you want to use for group creation.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        
        session_files = [f for f in os.listdir(user_session_dir) if f.endswith('.session')]
        
        if not session_files:
            await query.edit_message_text(
                "❌ **No Session Files Found**\n\n"
                f"Directory: `{user_session_dir}`\n"
                "No .session files found. Please login with an account first.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Main", callback_data="main_menu")]]),
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        # Debug session storage
        debug_session_storage(user_id)
        
        # Validate all sessions and get account details
        account_details = []
        await query.edit_message_text("🔍 **Validating accounts...**", parse_mode=ParseMode.MARKDOWN)
        
        for session_file in session_files:
            session_name = session_file.replace('.session', '')
            # Ensure session path is properly constructed with user ID
            session_path = ensure_user_session_path(user_id, session_name)
            
            print(f"Validating session for user {user_id}: {session_path}")
            print(f"Session file: {session_file}")
            print(f"Full session file path: {get_session_file_path(user_id, session_name)}")
            
            account_info = await validate_session(session_path, session_name, user_id)
            if account_info['valid']:
                account_details.append({
                    'session_name': session_name,
                    'session_path': session_path,
                    'phone': account_info['phone'],
                    'name': account_info['name'],
                    'username': account_info['username']
                })
                print(f"Valid account found for user {user_id}: {session_name}")
            else:
                print(f"Invalid account for user {user_id}: {session_name} - {account_info.get('reason', 'Unknown')}")
                # Try to recover from backup
                backup_dir = os.path.join(user_session_dir, "backups")
                if os.path.exists(backup_dir):
                    backup_files = [f for f in os.listdir(backup_dir) if f.startswith(session_name) and f.endswith('.session')]
                    if backup_files:
                        # Use the most recent backup
                        backup_files.sort(reverse=True)
                        backup_path = os.path.join(backup_dir, backup_files[0])
                        print(f"Attempting to recover session from backup: {backup_path}")
                        
                        # Try to restore from backup
                        try:
                            shutil.copy2(backup_path, os.path.join(user_session_dir, session_file))
                            print(f"Session restored from backup: {backup_path}")
                            
                            # Validate the restored session
                            restored_info = await validate_session(session_path, session_name, user_id)
                            if restored_info['valid']:
                                account_details.append({
                                    'session_name': session_name,
                                    'session_path': session_path,
                                    'phone': restored_info['phone'],
                                    'name': restored_info['name'],
                                    'username': restored_info['username']
                                })
                                continue
                        except Exception as e:
                            print(f"Failed to restore session from backup: {e}")
                
                # Remove invalid session files if no backup recovery
                try:
                    os.remove(os.path.join(user_session_dir, session_file))
                    print(f"Removed invalid session: {session_file}")
                except Exception as e:
                    print(f"Failed to remove invalid session {session_file}: {e}")
        
        if account_details:
            # Initialize selected accounts list if not exists
            context.user_data['selected_accounts'] = context.user_data.get('selected_accounts', [])
            context.user_data['available_accounts'] = account_details
            
            # Create multi-selection keyboard
            keyboard = []
            for i, acc in enumerate(account_details):
                # Check if account is already selected
                is_selected = any(sel['session_path'] == acc['session_path'] for sel in context.user_data['selected_accounts'])
                status = "✅" if is_selected else "⭕"
                
                # Escape special characters for safe display
                safe_name = escape_markdown(acc['name'])
                safe_username = escape_markdown(acc['username'])
                safe_phone = escape_markdown(acc['phone'])
                
                keyboard.append([InlineKeyboardButton(
                    f"{status} {safe_name} (@{safe_username}) - {safe_phone}", 
                    callback_data=f"toggle_account_{i}"
                )])
            
            keyboard.append([InlineKeyboardButton("✅ Select All", callback_data="select_all_accounts")])
            keyboard.append([InlineKeyboardButton("❌ Clear All", callback_data="clear_all_accounts")])
            
            if context.user_data['selected_accounts']:
                keyboard.append([InlineKeyboardButton("🚀 Continue with Selected", callback_data="continue_with_selected")])
            
            keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="start_creation" if query.data == "use_existing" else "view_accounts")])
            
            selected_count = len(context.user_data['selected_accounts'])
            await query.edit_message_text(
                f"📱 **Multi-Select Accounts**\n\n"
                f"**Selected:** {selected_count}/{len(account_details)} accounts\n\n"
                f"✅ = Selected, ⭕ = Not Selected\n"
                f"Click accounts to toggle selection.\n\n"
                f"💡 Select the accounts you want to use for group creation.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await query.edit_message_text(
                "❌ **No Valid Sessions Found**\n\nAll existing sessions seem invalid. Please add new accounts.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="start_creation" if query.data == "use_existing" else "view_accounts")]])
            )
    
    elif query.data == "add_new_account":
        login_keyboard = [
            [InlineKeyboardButton("📱 Manual Login", callback_data="manual_login")],
            [InlineKeyboardButton("📁 ZIP File Login", callback_data="zip_login")],
            [InlineKeyboardButton("🔙 Back", callback_data="start_creation")]
        ]
        
        await query.edit_message_text(
            "🔐 **Choose Login Method**\n\n"
            "📱 **Manual Login:** Enter phone number and complete OTP verification\n\n"
            "📁 **ZIP File Login:** Upload a ZIP file containing session files and account JSON files",
            reply_markup=InlineKeyboardMarkup(login_keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        context.user_data['conversation_state'] = LOGIN_METHOD_CHOICE
    
    elif query.data.startswith("toggle_account_"):
        account_index = int(query.data.split("_")[-1])
        available_accounts = context.user_data.get('available_accounts', [])
        selected_accounts = context.user_data.get('selected_accounts', [])
        
        if account_index < len(available_accounts):
            account = available_accounts[account_index]
            
            # Toggle selection
            is_selected = any(sel['session_path'] == account['session_path'] for sel in selected_accounts)
            
            if is_selected:
                # Remove from selection
                context.user_data['selected_accounts'] = [
                    sel for sel in selected_accounts if sel['session_path'] != account['session_path']
                ]
            else:
                # Add to selection
                context.user_data['selected_accounts'].append(account)
            
            # Update the keyboard
            keyboard = []
            for i, acc in enumerate(available_accounts):
                is_sel = any(sel['session_path'] == acc['session_path'] for sel in context.user_data['selected_accounts'])
                status = "✅" if is_sel else "⭕"
                
                # Escape special characters for safe display
                safe_name = escape_markdown(acc['name'])
                safe_username = escape_markdown(acc['username'])
                safe_phone = escape_markdown(acc['phone'])
                
                keyboard.append([InlineKeyboardButton(
                    f"{status} {safe_name} (@{safe_username}) - {safe_phone}", 
                    callback_data=f"toggle_account_{i}"
                )])
            
            keyboard.append([InlineKeyboardButton("✅ Select All", callback_data="select_all_accounts")])
            keyboard.append([InlineKeyboardButton("❌ Clear All", callback_data="clear_all_accounts")])
            
            if context.user_data['selected_accounts']:
                keyboard.append([InlineKeyboardButton("🚀 Continue with Selected", callback_data="continue_with_selected")])
            
            keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="start_creation")])
            
            selected_count = len(context.user_data['selected_accounts'])
            await query.edit_message_text(
                f"📱 **Multi-Select Accounts**\n\n"
                f"**Selected:** {selected_count}/{len(available_accounts)} accounts\n\n"
                f"✅ = Selected, ⭕ = Not Selected\n"
                f"Click accounts to toggle selection.\n\n"
                f"💡 Select the accounts you want to use for group creation.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
    
    elif query.data == "select_all_accounts":
        available_accounts = context.user_data.get('available_accounts', [])
        context.user_data['selected_accounts'] = available_accounts.copy()
        
        # Update keyboard to show all selected
        keyboard = []
        for i, acc in enumerate(available_accounts):
            # Escape special characters for safe display
            safe_name = escape_markdown(acc['name'])
            safe_username = escape_markdown(acc['username'])
            safe_phone = escape_markdown(acc['phone'])
            
            keyboard.append([InlineKeyboardButton(
                f"✅ {safe_name} (@{safe_username}) - {safe_phone}", 
                callback_data=f"toggle_account_{i}"
            )])
        
        keyboard.append([InlineKeyboardButton("✅ Select All", callback_data="select_all_accounts")])
        keyboard.append([InlineKeyboardButton("❌ Clear All", callback_data="clear_all_accounts")])
        keyboard.append([InlineKeyboardButton("🚀 Continue with Selected", callback_data="continue_with_selected")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="start_creation")])
        
        await query.edit_message_text(
            f"📱 **Multi-Select Accounts**\n\n"
            f"**Selected:** {len(available_accounts)}/{len(available_accounts)} accounts\n\n"
            f"✅ = Selected, ⭕ = Not Selected\n"
            f"Click accounts to toggle selection.\n\n"
            f"💡 All accounts are now selected!",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif query.data == "clear_all_accounts":
        available_accounts = context.user_data.get('available_accounts', [])
        context.user_data['selected_accounts'] = []
        
        # Update keyboard to show none selected
        keyboard = []
        for i, acc in enumerate(available_accounts):
            # Escape special characters for safe display
            safe_name = escape_markdown(acc['name'])
            safe_username = escape_markdown(acc['username'])
            safe_phone = escape_markdown(acc['phone'])
            
            keyboard.append([InlineKeyboardButton(
                f"⭕ {safe_name} (@{safe_username}) - {safe_phone}", 
                callback_data=f"toggle_account_{i}"
            )])
        
        keyboard.append([InlineKeyboardButton("✅ Select All", callback_data="select_all_accounts")])
        keyboard.append([InlineKeyboardButton("❌ Clear All", callback_data="clear_all_accounts")])
        keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="start_creation")])
        
        await query.edit_message_text(
            f"📱 **Multi-Select Accounts**\n\n"
            f"**Selected:** 0/{len(available_accounts)} accounts\n\n"
            f"✅ = Selected, ⭕ = Not Selected\n"
            f"Click accounts to toggle selection.\n\n"
            f"💡 All selections cleared. Choose accounts to proceed.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    
    elif query.data == "continue_with_selected":
        selected_accounts = context.user_data.get('selected_accounts', [])
        
        if not selected_accounts:
            await query.answer("❌ Please select at least one account first!", show_alert=True)
            return
        
        await query.edit_message_text(
            f"✅ **Accounts Selected**\n\n"
            f"📱 **Selected:** {len(selected_accounts)} accounts\n\n"
            f"🔢 **How many groups per account?**\n"
            f"💡 *Same number will run on each selected account, simultaneously*",
            parse_mode=ParseMode.MARKDOWN
        )
        context.user_data['conversation_state'] = GET_GROUP_COUNT
    
    elif query.data == "zip_login":
        await query.edit_message_text(
            "📁 **ZIP File Login**\n\n"
            "Please upload your ZIP file containing session files and account JSON files.\n\n"
            "📋 **Required Structure:**\n"
            "```\n"
            "accounts.zip\n"
            "├── phonenumber.json\n"
            "├── phonenumber.session\n"
            "└── ...\n"
            "```\n\n"
            "💡 **JSON Structure:**\n"
            "```json\n"
            "{\n"
            '  "app_id": 2040,\n'
            '  "app_hash": "...",\n'
            '  "twoFA": "password",\n'
            '  "phone": "14582439992",\n'
            '  "user_id": 8347055970\n'
            "}\n"
            "```",
            parse_mode=ParseMode.MARKDOWN
        )
        context.user_data['conversation_state'] = UPLOAD_ZIP
    
    elif query.data == "cancel_process":
        user_id = query.from_user.id
        if ACTIVE_PROCESSES.get(user_id):
            # Set cancellation flag to stop the process
            CANCELLATION_REQUESTED[user_id] = True
            ACTIVE_PROCESSES[user_id] = False
            
            await query.edit_message_text(
                "🛑 **Process Cancelled!**\n\n"
                "The group creation process has been stopped.\n"
                "Collecting partial results...",
                parse_mode=ParseMode.MARKDOWN
            )
            
            # Wait a moment for threads to finish current operations
            await asyncio.sleep(3)
            
            # Send partial results
            await send_partial_results(update, context, user_id)
        else:
            await query.answer("❌ No active process to cancel!", show_alert=True)
    
    elif query.data.startswith("view_links_"):
        phone_number = query.data.replace("view_links_", "")
        user_id = query.from_user.id
        
        # Get the links file path
        links_file = f"{phone_number}_links.txt"
        
        if not os.path.exists(links_file):
            await query.answer("❌ No links file found for this account!", show_alert=True)
            return
        
        try:
            # Read the links file
            with open(links_file, 'r', encoding='utf-8') as f:
                links = [line.strip() for line in f if line.strip()]
            
            if not links:
                await query.answer("❌ No links found in the file!", show_alert=True)
                return
            
            # Get account statistics
            try:
                account_summary = get_account_summary(phone_number)
                total_groups = account_summary["total_groups_created"]
                account_name = account_summary["account_info"].get("name", "Unknown")
            except:
                total_groups = len(links)
                account_name = "Unknown"
            
            # Create the links message
            links_text = f"🔗 **Group Links for {phone_number}**\n\n"
            links_text += f"👤 **Account:** {escape_markdown(account_name)}\n"
            links_text += f"🏗️ **Total Groups:** {total_groups}\n"
            links_text += f"📁 **Links File:** {len(links)} links\n\n"
            
            # Add first few links
            for i, link in enumerate(links[:10], 1):
                links_text += f"🔗 **{i}.** {link}\n"
            
            if len(links) > 10:
                links_text += f"\n... and {len(links) - 10} more links\n\n"
                links_text += "💡 **Use the button below to download the complete file**"
            
            # Create keyboard
            keyboard = []
            if len(links) > 10:
                keyboard.append([InlineKeyboardButton("📁 Download All Links", callback_data=f"download_links_{phone_number}")])
            keyboard.append([InlineKeyboardButton("🔙 Back to Stats", callback_data="account_stats")])
            keyboard.append([InlineKeyboardButton("🏠 Main Menu", callback_data="main_menu")])
            
            await query.edit_message_text(
                links_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode=ParseMode.MARKDOWN
            )
            
        except Exception as e:
            await query.answer(f"❌ Error reading links file: {str(e)}", show_alert=True)
    
    elif query.data.startswith("download_links_"):
        phone_number = query.data.replace("download_links_", "")
        user_id = query.from_user.id
        
        # Get the links file path
        links_file = f"{phone_number}_links.txt"
        
        if not os.path.exists(links_file):
            await query.answer("❌ No links file found for this account!", show_alert=True)
            return
        
        try:
            # Send the file
            with open(links_file, 'rb') as file:
                await context.bot.send_document(
                    chat_id=user_id,
                    document=file,
                    filename=f"{phone_number}_group_links.txt",
                    caption=f"📁 **Complete Group Links File**\n\n📱 **Account:** {phone_number}\n🔗 **Total Links:** {len(open(links_file, 'r').readlines())}\n\n💡 **All groups created by this account**"
                )
            
            await query.answer("✅ Links file sent!", show_alert=True)
            
        except Exception as e:
            await query.answer(f"❌ Error sending file: {str(e)}", show_alert=True)
    
    elif query.data == "verify_channel":
        # Verify channel membership
        if await check_channel_membership(user_id, context):
            # Add user to verified users set
            VERIFIED_USERS.add(user_id)
            
            await query.edit_message_text(
                "✅ **Verification Successful!**\n\n"
                "🎉 **Welcome to the bot!**\n\n"
                "You can now access all features.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔐 Login Your Accounts", callback_data="start_creation")],
                    [InlineKeyboardButton("🚀 Start Group Creation", callback_data="view_accounts")],
                    [InlineKeyboardButton("📊 Bot Statistics", callback_data="bot_stats")],
                    [InlineKeyboardButton("👨‍💻 Developer Info", callback_data="developer_info")]
                ]),
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await query.answer(
                "❌ **Verification Failed!**\n\n"
                "Please join the channel first and then click Verify again.",
                show_alert=True
            )
    
    elif query.data == "account_stats":
        # Redirect to account stats command
        await account_stats_command(update, context)

async def handle_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Check if update has a message
    if not update.message:
        return
    
    if 'awaiting_admin_id' not in context.user_data:
        return
    
    action = context.user_data['awaiting_admin_id']
    try:
        user_id = int(update.message.text.strip())
        
        if action == 'add':
            if user_id not in ADMIN_IDS:
                ADMIN_IDS.append(user_id)
                config['ADMIN_IDS'] = ADMIN_IDS
                save_config(config)
                await update.message.reply_text(
                    f"✅ **Admin Added Successfully!**\n\n"
                    f"👤 User ID: `{user_id}`\n"
                    f"🎯 Role: Admin\n"
                    f"📊 Total Admins: {len(ADMIN_IDS)}",
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                await update.message.reply_text(
                    f"⚠️ **Already an Admin!**\n\nUser ID `{user_id}` is already an admin.",
                    parse_mode=ParseMode.MARKDOWN
                )
        
        elif action == 'remove':
            if user_id in ADMIN_IDS:
                ADMIN_IDS.remove(user_id)
                config['ADMIN_IDS'] = ADMIN_IDS
                save_config(config)
                await update.message.reply_text(
                    f"✅ **Admin Removed Successfully!**\n\n"
                    f"👤 User ID: `{user_id}`\n"
                    f"📊 Total Admins: {len(ADMIN_IDS)}",
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                await update.message.reply_text(
                    f"⚠️ **Not an Admin!**\n\nUser ID `{user_id}` is not currently an admin.",
                    parse_mode=ParseMode.MARKDOWN
                )
        
        del context.user_data['awaiting_admin_id']
        
    except ValueError:
        await update.message.reply_text(
            "❌ **Invalid User ID!**\n\n"
            "Please send a valid numeric user ID.\n\n"
            "💡 **Example:** 123456789",
            parse_mode=ParseMode.MARKDOWN
        )

@authorized
async def run_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if ACTIVE_PROCESSES.get(user_id):
        await update.message.reply_text("⚠️ You already have a process running.")
        return ConversationHandler.END
    context.user_data.clear()
    await update.message.reply_text("Please send the phone number of the account you want to use (e.g., +15551234567).")
    return GET_PHONE

async def handle_conversation_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Check if update has a message
    if not update.message:
        return
    
    # Handle admin ID input
    if 'awaiting_admin_id' in context.user_data:
        await handle_admin_input(update, context)
        return
    
    # Handle ZIP file upload
    if update.message.document and context.user_data.get('conversation_state') == UPLOAD_ZIP:
        if update.message.document.file_name.endswith('.zip'):
            file = await context.bot.get_file(update.message.document.file_id)
            zip_path = f"temp_{update.effective_user.id}.zip"
            await file.download_to_drive(zip_path)
            
            # Show loading sticker
            loading_msg = await show_loading(update, "📁 **Processing ZIP file...**")
            
            await update.message.reply_text("📁 **Processing ZIP file...**\n\nPlease wait while I extract and validate accounts.", parse_mode=ParseMode.MARKDOWN)
            await process_zip_accounts(update, context, zip_path)
            # Hide loading sticker after processing
            await hide_loading(loading_msg)
        else:
            await update.message.reply_text("❌ **Invalid File Type**\n\nPlease upload a .zip file containing your account data.", parse_mode=ParseMode.MARKDOWN)
        return
    
    # Handle conversation states
    state = context.user_data.get('conversation_state')
    if state == GET_PHONE:
        return await get_phone(update, context)
    elif state == GET_LOGIN_CODE:
        return await get_login_code(update, context)
    elif state == GET_2FA_PASS:
        return await get_2fa_pass(update, context)
    elif state == GET_GROUP_COUNT:
        return await get_group_count_and_start(update, context)

async def get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Check if update has a message
    if not update.message:
        return
    
    user_id, phone = update.effective_user.id, update.message.text.strip()
    session_name = phone.replace('+', '')
    user_session_dir = os.path.join(SESSIONS_DIR, str(user_id))
    session_path = os.path.join(user_session_dir, session_name)
    os.makedirs(user_session_dir, exist_ok=True)

    # Check if session exists and is valid
    if os.path.exists(f"{session_path}.session"):
        try:
            client = TelegramClient(session_path, API_ID, API_HASH)
            await client.connect()
            if await client.is_user_authorized():
                await client.disconnect()
                context.user_data['conversation_state'] = None
                return await send_login_success_details(update, context, session_path, phone)
            else:
                await client.disconnect()
                # Remove invalid session file
                os.remove(f"{session_path}.session")
        except Exception:
            # Remove corrupted session file
            if os.path.exists(f"{session_path}.session"):
                os.remove(f"{session_path}.session")
    
    # Start fresh login process
    client = TelegramClient(session_path, API_ID, API_HASH)
    await client.connect()
    try:
        # Send loading sticker before sending OTP
        loading_message = await update.message.reply_sticker(LOADING_STICKER_ID)
        await asyncio.sleep(1.2)  # Show for 1.2 seconds
        await loading_message.delete()

        sent_code = await client.send_code_request(phone)
        context.user_data.update({
            'login_client': client, 
            'login_phone': phone, 
            'login_hash': sent_code.phone_code_hash, 
            'session_path': session_path,
            'conversation_state': GET_LOGIN_CODE
        })
        await update.message.reply_text(
            "📨 **OTP Sent!**\n\n"
            "I've sent a verification code to your phone number.\n"
            "Please send me the code you received.\n\n"
            "💡 **Format:** Usually 5-6 digits",
            parse_mode=ParseMode.MARKDOWN
        )
        return GET_LOGIN_CODE
    except Exception as e:
        await update.message.reply_text(f"❌ **Login Failed!** Could not send code. Please check the phone number and try again.\n\n`Error: {e}`", parse_mode=ParseMode.MARKDOWN)
        await client.disconnect()
        context.user_data['conversation_state'] = None

async def get_login_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Check if update has a message
    if not update.message:
        return
    
    code, client, phone, code_hash = update.message.text.strip(), context.user_data['login_client'], context.user_data['login_phone'], context.user_data['login_hash']
    try:
        await client.sign_in(phone, code, phone_code_hash=code_hash)
        session_path = context.user_data['session_path']
        await client.disconnect()  # Disconnect after successful login
        context.user_data['conversation_state'] = None
        return await send_login_success_details(update, context, session_path, phone)
    except SessionPasswordNeededError:
        await update.message.reply_text(
            "🔐 **2FA Enabled**\n\n"
            "This account has two-factor authentication enabled.\n"
            "Please send me your 2FA password.",
            parse_mode=ParseMode.MARKDOWN
        )
        context.user_data['conversation_state'] = GET_2FA_PASS
        return GET_2FA_PASS
    except Exception as e:
        await update.message.reply_text(f"❌ **Login Failed!** The code was incorrect. Please try again.", parse_mode=ParseMode.MARKDOWN)
        await client.disconnect()
        context.user_data['conversation_state'] = GET_PHONE
        return GET_PHONE

async def get_2fa_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Check if update has a message
    if not update.message:
        return
    
    password, client, phone = update.message.text.strip(), context.user_data['login_client'], context.user_data['login_phone']
    try:
        await client.sign_in(password=password)
        session_path = context.user_data['session_path']
        await client.disconnect()  # Disconnect after successful 2FA login
        context.user_data['conversation_state'] = None
        return await send_login_success_details(update, context, session_path, phone)
    except Exception as e:
        error_message = str(e)
        if "PASSWORD_HASH_INVALID" in error_message or "password" in error_message.lower():
            await update.message.reply_text("❌ **Incorrect 2FA Password**\n\nThe password you entered is incorrect. Please try again.", parse_mode=ParseMode.MARKDOWN)
        else:
            await update.message.reply_text(f"❌ **Login Failed!** {error_message}\n\nPlease try again.", parse_mode=ParseMode.MARKDOWN)
        context.user_data['conversation_state'] = GET_2FA_PASS
        return GET_2FA_PASS

async def get_group_count_and_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Check if update has a message
    if not update.message:
        return
    
    try:
        count = int(update.message.text)
        if count > 50:
            await update.message.reply_text("⚠️ **Warning:** Creating more than 50 groups can lead to account limits. Proceeding with caution.")

        user_id = update.effective_user.id
        
        # Check if it's selected existing accounts
        if 'selected_accounts' in context.user_data:
            # Multiple selected existing accounts
            accounts = context.user_data['selected_accounts']
            # Convert to the format expected by run_group_creation_process
            formatted_accounts = []
            for acc in accounts:
                formatted_accounts.append({
                    'session_path': acc['session_path'],
                    'phone': acc['phone']
                })
            
            total_groups_all = count * len(formatted_accounts)
            await update.message.reply_text(
                f"✅ **Setup Complete!**\n\n"
                f"📱 **Accounts:** {len(formatted_accounts)} selected accounts\n"
                f"📊 **Groups per Account:** {count}\n"
                f"🧮 **Total Groups (All Accounts):** {total_groups_all}\n"
                f"⏱️ **Delay:** {FIXED_DELAY} seconds\n"
                f"💬 **Messages per Group:** {FIXED_MESSAGES_PER_GROUP}\n\n"
                f"⏳ **Safety Delay:** Starting in 20 seconds to prevent account freezing...", 
                parse_mode=ParseMode.MARKDOWN
            )
            
            # Show countdown timer
            await countdown_timer(update, context, 20, "Safety Delay - Process Initialization")
            
            await update.message.reply_text("🚀 **Starting group creation process now...**", parse_mode=ParseMode.MARKDOWN)
            
            # Per-account count: run same number on each account, simultaneously
            groups_per_account = count
            
            progress_queue, start_time = queue.Queue(), time.time()
            ACTIVE_PROCESSES[user_id] = True
            
            # Start workers for each account
            for i, account in enumerate(formatted_accounts):
                account_groups = groups_per_account
                if account_groups > 0:
                    worker_args = (
                        account, account_groups,
                        FIXED_MESSAGES_PER_GROUP, FIXED_DELAY, FIXED_MESSAGES, progress_queue, user_id
                    )
                    threading.Thread(target=lambda args=worker_args: asyncio.run(run_group_creation_process(*args)), daemon=True).start()
            
            asyncio.create_task(progress_updater(update, context, progress_queue, start_time, total_groups_all))
            
        # Check if it's ZIP accounts
        elif 'zip_accounts' in context.user_data:
            # Multiple accounts from ZIP
            accounts = context.user_data['zip_accounts']
            total_groups_all = count * len(accounts)
            await update.message.reply_text(
                f"✅ **Setup Complete!**\n\n"
                f"📱 **Accounts:** {len(accounts)} loaded accounts\n"
                f"📊 **Groups per Account:** {count}\n"
                f"🧮 **Total Groups (All Accounts):** {total_groups_all}\n"
                f"⏱️ **Delay:** {FIXED_DELAY} seconds\n"
                f"💬 **Messages per Group:** {FIXED_MESSAGES_PER_GROUP}\n\n"
                f"⏳ **Safety Delay:** Starting in 20 seconds to prevent account freezing...", 
                parse_mode=ParseMode.MARKDOWN
            )
            
            # Show countdown timer
            await countdown_timer(update, context, 20, "Safety Delay - Process Initialization")
            
            await update.message.reply_text("🚀 **Starting group creation process now...**", parse_mode=ParseMode.MARKDOWN)
            
            # Per-account count: run same number on each account, simultaneously
            groups_per_account = count
            
            progress_queue, start_time = queue.Queue(), time.time()
            ACTIVE_PROCESSES[user_id] = True
            
            # Start workers for each account
            for i, account in enumerate(accounts):
                account_groups = groups_per_account
                if account_groups > 0:
                    worker_args = (
                        account, account_groups,
                        FIXED_MESSAGES_PER_GROUP, FIXED_DELAY, FIXED_MESSAGES, progress_queue, user_id
                    )
                    threading.Thread(target=lambda args=worker_args: asyncio.run(run_group_creation_process(*args)), daemon=True).start()
            
            asyncio.create_task(progress_updater(update, context, progress_queue, start_time, total_groups_all))
            
        else:
            # Single account (newly logged in)
            account_info = context.user_data['account_info']
            await update.message.reply_text(
                f"✅ **Setup Complete!**\n\n"
                f"📱 **Account:** `{account_info['phone']}`\n"
                f"📊 **Groups to Create:** {count}\n"
                f"⏱️ **Delay:** {FIXED_DELAY} seconds\n"
                f"💬 **Messages per Group:** {FIXED_MESSAGES_PER_GROUP}\n\n"
                f"⏳ **Safety Delay:** Starting in 20 seconds to prevent account freezing...", 
                parse_mode=ParseMode.MARKDOWN
            )

            # Show countdown timer
            await countdown_timer(update, context, 20, "Safety Delay - Process Initialization")
            
            await update.message.reply_text("🚀 **Starting group creation process now...**", parse_mode=ParseMode.MARKDOWN)

            ACTIVE_PROCESSES[user_id] = True
            progress_queue, start_time = queue.Queue(), time.time()

            worker_args = (
                account_info, count,
                FIXED_MESSAGES_PER_GROUP, FIXED_DELAY, FIXED_MESSAGES, progress_queue, user_id
            )

            threading.Thread(target=lambda: asyncio.run(run_group_creation_process(*worker_args)), daemon=True).start()
            asyncio.create_task(progress_updater(update, context, progress_queue, start_time, count))
            
        context.user_data['conversation_state'] = None
        
    except (ValueError, KeyError):
        await update.message.reply_text("Please enter a valid number.")

async def progress_updater(update: Update, context: ContextTypes.DEFAULT_TYPE, progress_queue: queue.Queue, start_time: float, total_groups: int):
    user_id = update.effective_user.id
    
    # Create keyboard with cancel button
    cancel_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛑 Cancel Process", callback_data="cancel_process")]
    ])
    
    status_message = await context.bot.send_message(
        chat_id=user_id, 
        text="🚀 **Starting process...**\n\n🛑 **Use the button below to cancel if needed**", 
        reply_markup=cancel_keyboard,
        parse_mode=ParseMode.MARKDOWN
    )
    created_count = 0

    while True:
        try:
            item = progress_queue.get_nowait()
            if isinstance(item, str) and item.startswith("DONE"):
                results = json.loads(item.split(':', 1)[1])
                time_taken = time.strftime("%H:%M:%S", time.gmtime(time.time() - start_time))
                final_report = f" **Process Complete!**\n\n **Time Taken:** {time_taken}\n\n"
                
                # Process each account result
                for res in results:
                    phone_number = res.get('phone_number', 'Unknown')
                    total_groups = res.get('total_groups_created', 0)
                    final_report += f" {res['account_details']}\n **Groups Created This Run:** {res['created_count']}\n **Total Groups (All Time):** {total_groups}\n\n"
                    
                    # Send account stats and cleanup data
                    if phone_number != 'Unknown':
                        stats_result = send_account_stats_and_cleanup(user_id, phone_number)
                        if stats_result['cleaned_up']:
                            final_report += f" **Data cleaned up for {phone_number}**\n\n"
                
                await context.bot.edit_message_text(
                    chat_id=user_id, 
                    message_id=status_message.message_id, 
                    text=final_report, 
                    parse_mode=ParseMode.MARKDOWN
                )
                
                # Send group files for each account
                for res in results:
                    phone_number = res.get('phone_number', 'Unknown')
                    if res.get('output_file') and os.path.exists(res['output_file']):
                        with open(res['output_file'], 'rb') as file:
                            await context.bot.send_document(
                                chat_id=user_id, 
                                document=file,
                                caption=f" **Group Links File**\n\n **Account:** {phone_number}\n **Groups Created This Run:** {res['created_count']}\n **Total Groups (All Time):** {res.get('total_groups_created', 0)}\n\n **All groups created by this account**\n\n **Note:** Stats have been automatically cleaned up from database.",
                                parse_mode=ParseMode.MARKDOWN
                            )
                break

            created_count += item
            percentage = (created_count / total_groups) * 100 if total_groups > 0 else 0
            progress_bar = "" * int(percentage // 10) + "" * (10 - int(percentage // 10))
            
            # Update progress with cancel button
            await context.bot.edit_message_text(
                chat_id=user_id, 
                message_id=status_message.message_id,
                text=f"⚙️ **Creating Groups...**\n\n📊 **Progress:** {progress_bar} {percentage:.1f}%\n🔢 **Created:** {created_count}/{total_groups}\n\n🛑 **Use the button below to cancel if needed**",
                reply_markup=cancel_keyboard,
                parse_mode=ParseMode.MARKDOWN
            )
        except queue.Empty: 
            await asyncio.sleep(2)
    ACTIVE_PROCESSES[user_id] = False

async def send_partial_results(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Send partial results when process is cancelled"""
    try:
        # Get user session directory to find account files
        user_session_dir = os.path.join(SESSIONS_DIR, str(user_id))
        if not os.path.exists(user_session_dir):
            await context.bot.send_message(
                chat_id=user_id,
                text="🛑 **Process Cancelled!**\n\nNo account data found.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        # Find all links files for this user's accounts
        links_files = []
        for file in os.listdir('.'):
            if file.endswith('_links.txt'):
                # Check if this file belongs to this user's accounts
                phone_number = file.replace('_links.txt', '')
                # Check if this phone number has a session file in user's directory
                session_file = f"{phone_number}.session"
                if os.path.exists(os.path.join(user_session_dir, session_file)):
                    links_files.append((phone_number, file))
        
        if not links_files:
            await context.bot.send_message(
                chat_id=user_id,
                text="🛑 **Process Cancelled!**\n\nNo groups were created before cancellation.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        
        # Send links files for each account
        for phone_number, links_file in links_files:
            if os.path.exists(links_file) and os.path.getsize(links_file) > 0:
                try:
                    with open(links_file, 'rb') as file:
                        await context.bot.send_document(
                            chat_id=user_id,
                            document=file,
                            filename=f"{phone_number}.txt",
                            caption=f"📋 **Group Links - Account {phone_number}**\n\n🔗 **Groups Created Before Cancellation**\n\n💡 **These are the groups created by this account before the process was cancelled**"
                        )
                except Exception as e:
                    print(f"Error sending cancelled links file for {phone_number}: {e}")
        
        # Send final cancellation message
        await context.bot.send_message(
            chat_id=user_id,
            text="✅ **Cancellation Complete!**\n\n📋 **All available group links have been sent above.**\n\n🔄 **You can start a new process anytime.**",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚀 Start New Process", callback_data="view_accounts")],
                [InlineKeyboardButton("🔙 Back to Main", callback_data="main_menu")]
            ]),
            parse_mode=ParseMode.MARKDOWN
        )
        
        # Clear cancellation flag
        if user_id in CANCELLATION_REQUESTED:
            del CANCELLATION_REQUESTED[user_id]
        
    except Exception as e:
        print(f"Error in send_partial_results: {e}")
        await context.bot.send_message(
            chat_id=user_id,
            text="🛑 **Process Cancelled!**\n\n❌ **Error retrieving results.**",
            parse_mode=ParseMode.MARKDOWN
        )

async def countdown_timer(update: Update, context: ContextTypes.DEFAULT_TYPE, seconds: int, message: str):
    """Display a countdown timer for process initialization"""
    countdown_message = await update.message.reply_text(
        f"⏳ **{message}**\n\n⏱️ **Starting in:** {seconds} seconds",
        parse_mode=ParseMode.MARKDOWN
    )
    
    for i in range(seconds - 1, 0, -1):
        await asyncio.sleep(1)
        try:
            await context.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=countdown_message.message_id,
                text=f"⏳ **{message}**\n\n⏱️ **Starting in:** {i} seconds",
                parse_mode=ParseMode.MARKDOWN
            )
        except:
            pass
    
    await asyncio.sleep(1)
    try:
        await context.bot.edit_message_text(
            chat_id=update.effective_chat.id,
            message_id=countdown_message.message_id,
            text=f"🚀 **{message}**\n\n✅ **Ready to start!**",
            parse_mode=ParseMode.MARKDOWN
        )
    except:
        pass
    
    return countdown_message

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ **Setup Cancelled**\n\nAll processes have been stopped.", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

@authorized
async def sessions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check and manage user sessions"""
    user_id = update.effective_user.id
    
    # Only owner and admins can access the bot
    if user_id not in OWNER_IDS and user_id not in ADMIN_IDS:
        await update.message.reply_text(
            "⛔ **Access Denied!**\n\n"
            "This bot is restricted to authorized users only.\n"
            "Contact the bot owner for access.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    user_session_dir = os.path.join(SESSIONS_DIR, str(user_id))
    
    if not os.path.exists(user_session_dir):
        await update.message.reply_text(
            "📭 **No Sessions Found**\n\nYou don't have any saved sessions.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    session_files = [f for f in os.listdir(user_session_dir) if f.endswith('.session')]
    if not session_files:
        await update.message.reply_text(
            "📭 **No Sessions Found**\n\nNo valid session files in your directory.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # Check backup directory
    backup_dir = os.path.join(user_session_dir, "backups")
    backup_count = 0
    if os.path.exists(backup_dir):
        backup_count = len([f for f in os.listdir(backup_dir) if f.endswith('.session')])
    
    sessions_text = f"🔐 **Your Sessions**\n\n📁 **Total Sessions:** {len(session_files)}\n📦 **Backups:** {backup_count}\n\n"
    
    for i, session_file in enumerate(session_files[:10], 1):
        session_name = session_file.replace('.session', '')
        session_path = os.path.join(user_session_dir, session_name)
        
        # Get file info
        try:
            file_size = os.path.getsize(f"{session_path}.session")
            size_mb = file_size / (1024 * 1024)
            sessions_text += f"📱 `{i}.` {session_name} ({size_mb:.2f} MB)\n"
        except:
            sessions_text += f"📱 `{i}.` {session_name} (Unknown size)\n"
    
    if len(session_files) > 10:
        sessions_text += f"\n... and {len(session_files) - 10} more sessions"
    
    await update.message.reply_text(
        sessions_text,
        parse_mode=ParseMode.MARKDOWN
    )

@authorized
async def health_check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check the health of all user sessions"""
    user_id = update.effective_user.id
    
    # Only owner and admins can access the bot
    if user_id not in OWNER_IDS and user_id not in ADMIN_IDS:
        await update.message.reply_text(
            "⛔ **Access Denied!**\n\n"
            "This bot is restricted to authorized users only.\n"
            "Contact the bot owner for access.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    user_session_dir = os.path.join(SESSIONS_DIR, str(user_id))
    
    if not os.path.exists(user_session_dir):
        await update.message.reply_text(
            "📭 **No Sessions Found**\n\nYou don't have any saved sessions.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    session_files = [f for f in os.listdir(user_session_dir) if f.endswith('.session')]
    if not session_files:
        await update.message.reply_text(
            "📭 **No Sessions Found**\n\nNo valid session files in your directory.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # Show loading sticker
    loading_msg = await show_loading(update, "🔍 **Checking session health...**")
    
    await update.message.reply_text("🔍 **Checking session health...**", parse_mode=ParseMode.MARKDOWN)
    
    healthy_sessions = []
    unhealthy_sessions = []
    
    for session_file in session_files:
        session_name = session_file.replace('.session', '')
        session_path = ensure_user_session_path(user_id, session_name)
        
        account_info = await validate_session(session_path, session_name, user_id)
        if account_info['valid']:
            healthy_sessions.append({
                'name': session_name,
                'details': account_info
            })
        else:
            unhealthy_sessions.append({
                'name': session_name,
                'reason': account_info.get('reason', 'Unknown error')
            })
    
    health_report = f"🏥 **Session Health Report**\n\n"
    health_report += f"✅ **Healthy Sessions:** {len(healthy_sessions)}\n"
    health_report += f"❌ **Unhealthy Sessions:** {len(unhealthy_sessions)}\n\n"
    
    if healthy_sessions:
        health_report += "✅ **Working Sessions:**\n"
        for session in healthy_sessions[:5]:
            details = session['details']
            # Escape special characters for safe display
            safe_name = escape_markdown(details['name'])
            safe_username = escape_markdown(details['username'])
            health_report += f"   📱 {session['name']} - {safe_name} (@{safe_username})\n"
    
    if unhealthy_sessions:
        health_report += "\n❌ **Problem Sessions:**\n"
        for session in unhealthy_sessions[:5]:
            # Escape special characters for safe display
            safe_reason = escape_markdown(session['reason'])
            health_report += f"   📱 {session['name']} - {safe_reason}\n"
    
    if len(healthy_sessions) > 5:
        health_report += f"\n... and {len(healthy_sessions) - 5} more healthy sessions"
    
    if len(unhealthy_sessions) > 5:
        health_report += f"\n... and {len(unhealthy_sessions) - 5} more problem sessions"
    
    # Hide loading sticker before showing results
    await hide_loading(loading_msg)
    
    await update.message.reply_text(
        health_report,
        parse_mode=ParseMode.MARKDOWN
    )

@authorized
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show help and features information"""
    user_id = update.effective_user.id
    
    # Only owner and admins can access the bot
    if user_id not in OWNER_IDS and user_id not in ADMIN_IDS:
        await update.message.reply_text(
            "⛔ **Access Denied!**\n\n"
            "This bot is restricted to authorized users only.\n"
            "Contact the bot owner for access.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    help_text = (
        "ℹ️ **Help & Features**\n\n"
        "🚀 **Start Group Creation**\n"
        "   • Login with phone number\n"
        "   • Automatic OTP handling\n"
        "   • Session file provided\n\n"
        "👥 **View Accounts** (Admins only)\n"
        "   • See all logged admin accounts\n"
        "   • Account details and status\n\n"
        "📊 **Statistics**\n"
        "   • Bot usage statistics\n"
        "   • Admin and account counts\n\n"
        "⚙️ **Admin Management** (Owner only)\n"
        "   • Add/remove admins\n"
        "   • List current admins\n"
        "   • Channel verification setup\n\n"
        "🔐 **Security Features**\n"
        "   • Role-based access control\n"
        "   • Secure session storage\n"
        "   • Admin-only account access\n"
        "   • Restricted to authorized users only\n\n"
        "📢 **Access Control**\n"
        "   • Only owner and admins can use the bot\n"
        "   • Contact bot owner for access\n"
        "   • Secure admin management system\n\n"
        "💡 **Available Commands:**\n"
        "   • /start - Main menu\n"
        "   • /sessions - Account management\n"
        "   • /health - Account health check\n"
        "   • /help - This help message\n"
        "   • /stats - Bot statistics\n"
        "   • /accountstats - Account statistics & links\n"
        "   • /create - Start group creation\n"
        "   • /cancel - Stop current process\n"
        "   • /setup_channel - Setup channel verification (Admin)\n"
        "   • /channel_info - View channel settings (Admin)"
    )
    
    await update.message.reply_text(
        help_text,
        parse_mode=ParseMode.MARKDOWN
    )

@authorized
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show bot statistics"""
    user_id = update.effective_user.id
    
    # Only owner and admins can access the bot
    if user_id not in OWNER_IDS and user_id not in ADMIN_IDS:
        await update.message.reply_text(
            "⛔ **Access Denied!**\n\n"
            "This bot is restricted to authorized users only.\n"
            "Contact the bot owner for access.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    total_admins = len(ADMIN_IDS)
    total_sessions = 0
    
    for admin_id in ADMIN_IDS:
        admin_session_dir = os.path.join(SESSIONS_DIR, str(admin_id))
        if os.path.exists(admin_session_dir):
            total_sessions += len([f for f in os.listdir(admin_session_dir) if f.endswith('.session')])
    
    stats_text = (
        f"📊 **Bot Statistics**\n\n"
        f"👨‍💼 **Total Admins:** {total_admins}\n"
        f"📱 **Logged Accounts:** {total_sessions}\n"
        f"⚙️ **Messages per Group:** {FIXED_MESSAGES_PER_GROUP}\n"
        f"⏱️ **Fixed Delay:** {FIXED_DELAY} seconds\n"
        f"🔄 **Active Processes:** {len([p for p in ACTIVE_PROCESSES.values() if p])}\n\n"
        f"🤖 **Bot Version:** 2.0 Enhanced\n"
        f"⚡ **Optimized for:** 20-minute group creation"
    )
    
    await update.message.reply_text(
        stats_text,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Main", callback_data="main_menu")]]),
        parse_mode=ParseMode.MARKDOWN
    )

@authorized
async def account_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show detailed account statistics and links"""
    user_id = update.effective_user.id
    
    # Only owner and admins can access the bot
    if user_id not in OWNER_IDS and user_id not in ADMIN_IDS:
        await update.message.reply_text(
            "⛔ **Access Denied!**\n\n"
            "This bot is restricted to authorized users only.\n"
            "Contact the bot owner for access.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    user_session_dir = os.path.join(SESSIONS_DIR, str(user_id))
    
    if not os.path.exists(user_session_dir):
        await update.message.reply_text(
            "📭 **No Accounts Found**\n\nYou don't have any logged-in accounts.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    session_files = [f for f in os.listdir(user_session_dir) if f.endswith('.session')]
    if not session_files:
        await update.message.reply_text(
            "📭 **No Valid Sessions Found**\n\nNo valid session files found.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # Show loading sticker
    loading_msg = await show_loading(update, "🔍 **Gathering account statistics...**")
    
    await update.message.reply_text("🔍 **Gathering account statistics...**", parse_mode=ParseMode.MARKDOWN)
    
    accounts_stats = []
    total_groups_created = 0
    
    for session_file in session_files:
        session_name = session_file.replace('.session', '')
        
        # Get account statistics
        try:
            account_summary = get_account_summary(session_name)
            accounts_stats.append(account_summary)
            total_groups_created += account_summary["total_groups_created"]
        except Exception as e:
            print(f"Error getting stats for {session_name}: {e}")
            accounts_stats.append({
                "phone_number": session_name,
                "total_groups_created": 0,
                "groups_created_today": 0,
                "total_links_in_file": 0,
                "last_updated": "Unknown",
                "account_info": {}
            })
    
    if not accounts_stats:
        await update.message.reply_text(
            "❌ **No Account Statistics Available**\n\nCould not retrieve statistics for any accounts.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # Create detailed statistics message
    stats_text = f"📊 **Account Statistics Report**\n\n"
    stats_text += f"📱 **Total Accounts:** {len(accounts_stats)}\n"
    stats_text += f"🏗️ **Total Groups Created:** {total_groups_created}\n\n"
    
    # Show individual account stats
    for i, account in enumerate(accounts_stats[:10], 1):  # Limit to first 10
        phone = account["phone_number"]
        total_groups = account["total_groups_created"]
        today_groups = account["groups_created_today"]
        total_links = account["total_links_in_file"]
        last_updated = account["last_updated"]
        
        # Get account name if available
        account_name = "Unknown"
        if account["account_info"] and account["account_info"].get("name"):
            account_name = account["account_info"]["name"]
        
        stats_text += f"📱 **{i}. {phone}**\n"
        stats_text += f"   👤 **Name:** {escape_markdown(account_name)}\n"
        stats_text += f"   🏗️ **Total Groups:** {total_groups}\n"
        stats_text += f"   📅 **Today:** {today_groups}\n"
        stats_text += f"   🔗 **Links File:** {total_links} links\n"
        stats_text += f"   ⏰ **Last Updated:** {last_updated}\n\n"
    
    if len(accounts_stats) > 10:
        stats_text += f"... and {len(accounts_stats) - 10} more accounts\n\n"
    
    # Create keyboard with options
    keyboard = []
    
    # Add buttons for each account to view their links file
    for i, account in enumerate(accounts_stats[:5]):  # Limit to first 5 for keyboard
        if account["total_links_in_file"] > 0:
            keyboard.append([InlineKeyboardButton(
                f"📁 {account['phone_number']} Links ({account['total_links_in_file']})", 
                callback_data=f"view_links_{account['phone_number']}"
            )])
    
    keyboard.append([InlineKeyboardButton("🔙 Back to Main", callback_data="main_menu")])
    
    # Hide loading sticker before showing results
    await hide_loading(loading_msg)
    
    await update.message.reply_text(
        stats_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

@authorized
async def create_groups_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start group creation process directly via command"""
    user_id = update.effective_user.id
    
    # Only owner and admins can access the bot
    if user_id not in OWNER_IDS and user_id not in ADMIN_IDS:
        await update.message.reply_text(
            "⛔ **Access Denied!**\n\n"
            "This bot is restricted to authorized users only.\n"
            "Contact the bot owner for access.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    if ACTIVE_PROCESSES.get(user_id):
        await update.message.reply_text(
            "⚠️ **Process Already Running!**\n\nYou already have a group creation process active.\n\n🛑 **Use the cancel button below to stop it**",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🛑 Cancel Running Process", callback_data="cancel_process")],
                [InlineKeyboardButton("🔙 Back to Main", callback_data="main_menu")]
            ]),
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # Check for existing accounts
    user_session_dir = os.path.join(SESSIONS_DIR, str(user_id))
    if not os.path.exists(user_session_dir):
        await update.message.reply_text(
            "❌ **No Accounts Found**\n\nPlease use /start to login with an account first.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    session_files = [f for f in os.listdir(user_session_dir) if f.endswith('.session')]
    if not session_files:
        await update.message.reply_text(
            "❌ **No Valid Sessions Found**\n\nPlease use /start to login with an account first.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # Show account selection
    account_keyboard = [
        
        [InlineKeyboardButton("➕ Add New Account", callback_data="add_new_account")],

    ]
    
    await update.message.reply_text(
        f"🔐 **Account Selection**\n\n"
        f"📱 **Found {len(session_files)} existing accounts**\n\n"
        f"Choose to use existing accounts or add new ones:",
        reply_markup=InlineKeyboardMarkup(account_keyboard),
        parse_mode=ParseMode.MARKDOWN
    )
    
    context.user_data.clear()
    context.user_data['conversation_state'] = LOGIN_METHOD_CHOICE

@admin_only
async def setup_channel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Setup channel verification settings (admin only)"""
    if len(context.args) < 2:
        await update.message.reply_text(
            "⚙️ **Channel Setup**\n\n"
            "Usage: `/setup_channel @channel_username https://t.me/channel_username`\n\n"
            "Example: `/setup_channel @MyChannel https://t.me/MyChannel`\n\n"
            "💡 **Note:** Only admins can change channel settings",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    global REQUIRED_CHANNEL, CHANNEL_LINK
    
    channel_username = context.args[0]
    channel_link = context.args[1]
    
    # Validate channel username format
    if not channel_username.startswith('@'):
        await update.message.reply_text(
            "❌ **Invalid Channel Username!**\n\n"
            "Channel username must start with @\n"
            "Example: @MyChannel",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # Validate channel link format
    if not channel_link.startswith('https://t.me/'):
        await update.message.reply_text(
            "❌ **Invalid Channel Link!**\n\n"
            "Channel link must be in format: https://t.me/channel_username\n"
            "Example: https://t.me/MyChannel",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    # Update global variables
    REQUIRED_CHANNEL = channel_username
    CHANNEL_LINK = channel_link
    
    await update.message.reply_text(
        f"✅ **Channel Setup Complete!**\n\n"
        f"📢 **Channel:** {channel_username}\n"
        f"🔗 **Link:** {channel_link}\n\n"
        f"🔄 **Bot will now require users to join this channel**",
        parse_mode=ParseMode.MARKDOWN
    )

@admin_only
async def channel_info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show current channel verification settings (admin only)"""
    await update.message.reply_text(
        f"📢 **Current Channel Settings**\n\n"
        f"🔗 **Required Channel:** {REQUIRED_CHANNEL}\n"
        f"🌐 **Channel Link:** {CHANNEL_LINK}\n\n"
        f"💡 **To change settings, use:**\n"
        f"`/setup_channel @new_channel https://t.me/new_channel`",
        parse_mode=ParseMode.MARKDOWN
    )

def main():
    application = Application.builder().token(config['BOT_TOKEN']).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("sessions", sessions_command))
    application.add_handler(CommandHandler("health", health_check_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("accountstats", account_stats_command))
    application.add_handler(CommandHandler("create", create_groups_command))
    application.add_handler(CommandHandler("setup_channel", setup_channel_command))
    application.add_handler(CommandHandler("channel_info", channel_info_command))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_conversation_input))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_conversation_input))
    application.add_handler(CommandHandler("cancel", cancel))

    application.run_polling()

if __name__ == '__main__':
    main()
