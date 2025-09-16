# Save this file as BigBotFinal.py
import asyncio
import random
import json
import time
import os
from datetime import datetime
from telethon import TelegramClient
from telethon.tl.functions.channels import CreateChannelRequest
from telethon.tl.functions.messages import ExportChatInviteRequest
from telethon.tl.functions.account import UpdateProfileRequest
from telethon.errors.rpcerrorlist import FloodWaitError, ChatAdminRequiredError

# Import ACTIVE_PROCESSES and CANCELLATION_REQUESTED from the main bot file
try:
    from telegram_bot import ACTIVE_PROCESSES, CANCELLATION_REQUESTED
except ImportError:
    # Fallback if imported directly
    ACTIVE_PROCESSES = {}
    CANCELLATION_REQUESTED = {}

API_ID = 22566208

API_HASH = "fa18dcf886c0d78f20e849f54be62940"

def get_user_folder_path(user_id, phone_number):
    """Get the user-specific folder path for storing account data"""
    sessions_dir = 'sessions'
    user_folder = os.path.join(sessions_dir, str(user_id))
    os.makedirs(user_folder, exist_ok=True)
    return user_folder

def get_account_stats_file(user_id, phone_number):
    """Get the stats file path for an account in user's folder"""
    user_folder = get_user_folder_path(user_id, phone_number)
    return os.path.join(user_folder, f"{phone_number.replace('+', '')}_stats.json")

def get_account_groups_file(user_id, phone_number, groups_count):
    """Get the groups file path with format: accountnumber_groupscount_date.txt"""
    user_folder = get_user_folder_path(user_id, phone_number)
    date_str = datetime.now().strftime("%Y%m%d")
    clean_phone = phone_number.replace('+', '')
    filename = f"{clean_phone}_{groups_count}_{date_str}.txt"
    return os.path.join(user_folder, filename)

def load_account_stats(user_id, phone_number):
    """Load existing account statistics from user's folder"""
    stats_file = get_account_stats_file(user_id, phone_number)
    if os.path.exists(stats_file):
        try:
            with open(stats_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            pass
    return {
        "total_groups_created": 0,
        "groups_created_today": 0,
        "all_group_links": [],
        "last_updated": "",
        "account_info": {}
    }

def save_account_stats(user_id, phone_number, stats):
    """Save account statistics to user's folder"""
    stats_file = get_account_stats_file(user_id, phone_number)
    stats["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(stats_file, 'w', encoding='utf-8') as f:
            json.dump(stats, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Failed to save stats for {phone_number}: {e}")

def save_group_link(user_id, phone_number, group_title, invite_link, groups_file_path):
    """Save a group link to the account's groups file in user's folder"""
    try:
        with open(groups_file_path, 'a', encoding='utf-8') as f:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"[{timestamp}] {group_title}: {invite_link}\n")
        print(f"Link saved to {groups_file_path}")
    except Exception as e:
        print(f"Failed to save link to {groups_file_path}: {e}")

def cleanup_account_data(user_id, phone_number):
    """Delete account stats and related files after sending data"""
    try:
        # Delete stats file
        stats_file = get_account_stats_file(user_id, phone_number)
        if os.path.exists(stats_file):
            os.remove(stats_file)
            print(f"Deleted stats file: {stats_file}")
        
        # Find and delete groups files for this account
        user_folder = get_user_folder_path(user_id, phone_number)
        clean_phone = phone_number.replace('+', '')
        
        for filename in os.listdir(user_folder):
            if filename.startswith(f"{clean_phone}_") and filename.endswith('.txt'):
                file_path = os.path.join(user_folder, filename)
                os.remove(file_path)
                print(f"Deleted groups file: {file_path}")
                
    except Exception as e:
        print(f"Error cleaning up account data for {phone_number}: {e}")

async def safe_sleep(seconds: int, reason: str = ""):
    """Safe sleep with logging"""
    if seconds > 0:
        print(f"Sleeping {seconds}s - {reason}")
        await asyncio.sleep(seconds)

async def update_account_bio(client, current_bio=""):
    """Update account bio with the specified text"""
    try:
        # The text to add to bio
        nexo_text = "\n\nCreated by @NexoUnion Bot services\nDeveloper @OldGcHub"
        
        # Check if the text is already in the bio
        if "Created by @NexoUnion Bot services" in current_bio:
            print("Bio already contains NexoUnion text, skipping update")
            return True
        
        # Prepare new bio (keep existing bio and add our text)
        new_bio = current_bio.strip() + nexo_text
        
        # Telegram bio limit is 70 characters, so we need to handle this
        if len(new_bio) > 70:
            # If current bio + our text exceeds limit, prioritize our text
            if len(nexo_text.strip()) <= 70:
                new_bio = nexo_text.strip()
            else:
                # If even our text is too long, truncate it
                new_bio = nexo_text.strip()[:70]
        
        # Update the bio
        await client(UpdateProfileRequest(about=new_bio))
        print(f"Successfully updated account bio")
        return True
        
    except Exception as e:
        print(f"Failed to update account bio: {e}")
        return False

async def account_worker(account_info, groups_to_create, messages_to_send, delay, progress_queue, user_id=None):
    session_path = account_info['session_path']
    phone_number = account_info.get('phone', 'session').replace('+', '')
    account_details = "Could not log in."
    total_created_this_run = 0
    
    # Load existing account statistics
    account_stats = load_account_stats(user_id, phone_number)
    
    # Get the groups file path with current count and date
    groups_file = get_account_groups_file(user_id, phone_number, groups_to_create)
    
    # Create groups file if it doesn't exist
    if not os.path.exists(groups_file):
        with open(groups_file, 'w', encoding='utf-8') as f:
            f.write(f"Group Links for Account: {phone_number}\n")
            f.write(f"Created on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Target Groups: {groups_to_create}\n")
            f.write("=" * 50 + "\n\n")

    try:
        # Connect to client
        client = TelegramClient(session_path, API_ID, API_HASH)
        await client.connect()
        
        # Check if authorized
        if not await client.is_user_authorized():
            print(f"Account {phone_number} not authorized")
            await client.disconnect()
            return {
                "created_count": 0,
                "account_details": "Session expired or invalid",
                "output_file": None,
                "total_groups_created": account_stats["total_groups_created"]
            }
        
        # Get account details (but don't log them to avoid detection)
        try:
            me = await client.get_me()
            account_details = (
                f"👤 **Name:** {me.first_name} {me.last_name or ''}\n"
                f"🔖 **Username:** @{me.username or 'N/A'}\n"
                f"🆔 **ID:** `{me.id}`"
            )
            
            # Update account info in stats
            account_stats["account_info"] = {
                "name": f"{me.first_name} {me.last_name or ''}".strip(),
                "username": me.username or 'N/A',
                "id": me.id
            }
            
            print(f"Account loaded: {me.first_name} (@{me.username})")
            
            # Update account bio with NexoUnion text
            current_bio = me.about or ""
            await update_account_bio(client, current_bio)
            
        except Exception as e:
            print(f"Could not get account details: {e}")
            account_details = "Account details unavailable"
        
        # Reduced initial delay after login to avoid immediate automation detection
        print("Waiting 20 seconds after login to avoid account freezing...")
        await safe_sleep(20, "Reduced safety delay after login")
        
        for i in range(groups_to_create):
            # Check for cancellation only if user clicked cancel button
            if user_id and CANCELLATION_REQUESTED.get(user_id, False):
                print(f"User requested cancellation for {phone_number}, stopping at group {i+1}")
                break
                
            try:
                # Random group title to avoid pattern detection
                adjectives = ['Golden', 'Silent', 'Hidden', 'Secret', 'Private', 'Elite', 'Premium', 'Exclusive']
                nouns = ['Oasis', 'Sanctuary', 'Valley', 'Garden', 'Haven', 'Retreat', 'Club', 'Society']
                group_title = f"{random.choice(adjectives)} {random.choice(nouns)} {random.randint(100, 999)}"
                
                print(f"Creating group {i+1}/{groups_to_create}: {group_title}")
                
                # Create group with random delay
                result = await client(CreateChannelRequest(
                    title=group_title, 
                    about="Welcome to our community!\n\nCreated by @NexoUnion Bot services\nDeveloper @OldGcHub", 
                    megagroup=True
                ))
                new_group = result.chats[0]
                
                # Reduced delay after group creation
                await safe_sleep(random.randint(5, 10), f"Delay after creating group {group_title}")
                
                # Get invite link
                try:
                    invite_result = await client(ExportChatInviteRequest(new_group.id))
                    invite_link = invite_result.link
                    
                    # Save link to file
                    save_group_link(user_id, phone_number, group_title, invite_link, groups_file)
                    
                    # Update account statistics
                    account_stats["all_group_links"].append({
                        "title": group_title,
                        "link": invite_link,
                        "created_at": time.strftime("%Y-%m-%d %H:%M:%S")
                    })
                    account_stats["total_groups_created"] += 1
                    account_stats["groups_created_today"] += 1
                    
                    print(f"Invite link generated for {group_title}")
                except ChatAdminRequiredError:
                    print(f"Could not export invite for {group_title} - admin rights issue")
                    continue
                
                # Send messages with proper delays
                for j, msg in enumerate(messages_to_send):
                    try:
                        await client.send_message(new_group.id, msg)
                        print(f"Sent message {j+1}/{len(messages_to_send)} to {group_title}")
                        
                        # Random delay between messages (2-5 seconds)
                        if j < len(messages_to_send) - 1:
                            msg_delay = random.randint(2, 5)
                            await safe_sleep(msg_delay, f"Delay between messages in {group_title}")
                    except Exception as e:
                        print(f"Failed to send message {j+1} to {group_title}: {e}")
                        continue
                
                # Note: Commands message removed - should not be sent to groups automatically
                # Users can access bot commands directly in the bot chat
                
                total_created_this_run += 1
                progress_queue.put(1)
                
                # Reduced delay between groups (5-10 seconds) for faster creation
                if i < groups_to_create - 1:
                    group_delay = random.randint(5, 10)
                    await safe_sleep(group_delay, f"Delay before next group")
                
                # Reduced safety delay every 5 groups (10-20 seconds)
                if (i + 1) % 5 == 0:
                    safety_delay = random.randint(10, 20)
                    await safe_sleep(safety_delay, f"Safety delay after {i+1} groups")
                
            except FloodWaitError as fwe:
                print(f"FloodWait for {group_title}: sleeping {fwe.seconds}s")
                await safe_sleep(fwe.seconds + 10, "FloodWait recovery")
                continue
            except Exception as e:
                print(f"Error creating group {group_title}: {e}")
                continue
        
        # Save updated account statistics
        save_account_stats(user_id, phone_number, account_stats)
        
        # Final delay before disconnecting
        await safe_sleep(10, "Final delay before disconnecting")
        
    except Exception as e:
        print(f"FATAL ERROR for {phone_number}: {e}")
    finally:
        try:
            await client.disconnect()
        except:
            pass
        
        return {
            "created_count": total_created_this_run,
            "account_details": account_details,
            "output_file": groups_file if total_created_this_run > 0 else None,
            "total_groups_created": account_stats["total_groups_created"],
            "phone_number": phone_number
        }

async def run_group_creation_process(account_config, total_groups, msgs_per_group, delay, messages, progress_queue, user_id=None):
    results = await asyncio.gather(account_worker(account_config, total_groups, messages[:msgs_per_group], delay, progress_queue, user_id))
    progress_queue.put(f"DONE:{json.dumps(results)}")

def get_account_summary(user_id, phone_number):
    """Get a summary of account statistics from user's folder"""
    stats = load_account_stats(user_id, phone_number)
    
    # Count total links in all groups files for this account
    total_links = 0
    user_folder = get_user_folder_path(user_id, phone_number)
    clean_phone = phone_number.replace('+', '')
    
    try:
        for filename in os.listdir(user_folder):
            if filename.startswith(f"{clean_phone}_") and filename.endswith('.txt'):
                file_path = os.path.join(user_folder, filename)
                with open(file_path, 'r', encoding='utf-8') as f:
                    # Count lines that contain links (skip header lines)
                    content = f.read()
                    total_links += content.count('https://t.me/')
    except Exception as e:
        print(f"Error counting links for {phone_number}: {e}")
    
    return {
        "phone_number": phone_number,
        "total_groups_created": stats["total_groups_created"],
        "groups_created_today": stats["groups_created_today"],
        "total_links_in_file": total_links,
        "last_updated": stats["last_updated"],
        "account_info": stats["account_info"]
    }

def send_account_stats_and_cleanup(user_id, phone_number):
    """Send account statistics and then cleanup the data"""
    try:
        # Get account summary
        summary = get_account_summary(user_id, phone_number)
        
        # Find all groups files for this account
        user_folder = get_user_folder_path(user_id, phone_number)
        clean_phone = phone_number.replace('+', '')
        groups_files = []
        
        for filename in os.listdir(user_folder):
            if filename.startswith(f"{clean_phone}_") and filename.endswith('.txt'):
                groups_files.append(os.path.join(user_folder, filename))
        
        # Cleanup data after getting summary
        cleanup_account_data(user_id, phone_number)
        
        return {
            "summary": summary,
            "groups_files": groups_files,
            "cleaned_up": True
        }
        
    except Exception as e:
        print(f"Error in send_account_stats_and_cleanup for {phone_number}: {e}")
        return {
            "summary": None,
            "groups_files": [],
            "cleaned_up": False,
            "error": str(e)
        }