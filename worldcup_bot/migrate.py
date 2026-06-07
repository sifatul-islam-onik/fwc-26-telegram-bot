import re

with open('e:/CSE/CODES/github/fwc-26-telegram-bot/worldcup_bot/bot.py', 'r', encoding='utf-8') as f:
    code = f.read()

# 1. Add chat_id = update.effective_chat.id to all commands
def add_chat_id(m):
    return m.group(0) + '\n    chat_id = update.effective_chat.id\n'

code = re.sub(r'async def \w+_cmd\(update: Update, context: ContextTypes\.DEFAULT_TYPE\):', add_chat_id, code)

# 2. Fix _toggle_setting signature and logic
code = code.replace(
    'async def _toggle_setting(application, args, setting_key, label_on, label_off, current_label):',
    'async def _toggle_setting(application, chat_id, args, setting_key, label_on, label_off, current_label):'
)

# 3. Replace send_text calls to include chat_id
code = re.sub(r'await send_text\(context\.application, (.*?)\)', r'await send_text(context.application, chat_id, \1)', code)
code = re.sub(r'await send_text\(application, (.*?)\)', r'await send_text(application, chat_id, \1)', code)

# 4. Replace state functions
code = re.sub(r'state\.get_setting\((.*?)\)', r'state.get_setting(chat_id, \1)', code)
code = re.sub(r'state\.set_setting\((.*?)\)', r'state.set_setting(chat_id, \1)', code)
code = code.replace('state.get_favourite_teams()', 'state.get_favourite_teams(chat_id)')
code = code.replace('state.add_favourite_team(team)', 'state.add_favourite_team(chat_id, team)')
code = code.replace('state.remove_favourite_team(matched_team["id"])', 'state.remove_favourite_team(chat_id, matched_team["id"])')
code = code.replace('state.reset_settings()', 'state.reset_settings(chat_id)')

# 5. Fix _get_user_tz to take chat_id
code = code.replace('def _get_user_tz():', 'def _get_user_tz(chat_id):')
code = code.replace('_get_user_tz()', '_get_user_tz(chat_id)')

# 6. Fix error_handler send_text (doesn't have chat_id easily)
# Revert it manually
code = code.replace(
'''        if update and getattr(update, "effective_chat", None):
            import asyncio
            asyncio.create_task(send_text(context.application, chat_id, msg))''',
'''        if update and getattr(update, "effective_chat", None):
            import asyncio
            asyncio.create_task(send_text(context.application, update.effective_chat.id, msg))'''
)

# 7. Fix _toggle_setting calls in commands
code = re.sub(r'await _toggle_setting\(context\.application, context\.args, ', r'await _toggle_setting(context.application, chat_id, context.args, ', code)

# 8. Remove the old telegram_chat_id start logic
code = code.replace('state.set_setting(chat_id, "telegram_chat_id", str(chat_id))\n    \n', '')

with open('e:/CSE/CODES/github/fwc-26-telegram-bot/worldcup_bot/bot.py', 'w', encoding='utf-8') as f:
    f.write(code)
