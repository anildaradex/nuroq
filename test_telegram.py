import asyncio
import sys
import os
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

# Load credentials from .env
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

async def test_gatekeeper():
    if not BOT_TOKEN or not CHAT_ID:
        print("❌ Error: TELEGRAM_TOKEN or TELEGRAM_CHAT_ID missing in .env file.")
        return

    print("🚀 Initializing Telegram Test Bot...")
    app = Application.builder().token(BOT_TOKEN).build()
    
    # Event to pause the script
    approval_event = asyncio.Event()

    async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        print(f"✅ Button Pressed: {query.data}")
        await query.edit_message_text(text=f"🔘 Received: {query.data}. Connection Verified!")
        approval_event.set() # Open the gate

    app.add_handler(CallbackQueryHandler(handle_button))

    async with app:
        await app.start() # Ensure internal state is started
        await app.updater.start_polling()
        print("🟢 Bot is now listening for your tap on Telegram...")
        
        # Build the Test UI
        keyboard = [[
            InlineKeyboardButton("✅ EXECUTE", callback_data='EXECUTE'),
            InlineKeyboardButton("❌ CANCEL", callback_data='CANCEL'),
        ]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        print(f"📡 Sending test message to Chat ID: {CHAT_ID}...")
        try:
            await app.bot.send_message(
                chat_id=CHAT_ID,
                text="📈 **AI TRADING BOT TEST**\n\nThis is a test signal. Tap a button below to verify the connection.",
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        except Exception as e:
            print(f"❌ Failed to send message: {e}")
            print("💡 Tip: Make sure you have started a chat with your bot on Telegram first!")
            return

        print("⏳ Waiting for you to tap a button on your phone (5 min timeout)...")
        try:
            await asyncio.wait_for(approval_event.wait(), timeout=300)
            print("✨ Test Successful! The bridge is working.")
        except asyncio.TimeoutError:
            print("⌛ Test Timed Out. No button was pressed.")
        
        await app.updater.stop()
        await app.stop() # Ensure state is cleaned up
    
    print("✨ Test Successful! The bridge is working.")

if __name__ == "__main__":
    asyncio.run(test_gatekeeper())
