# Premium Earn Bot

A complete Telegram earning bot with tasks, games, referral system, and withdrawal system.

## Features

- 💰 Tasks (Channel Join, Shortlink, Sponsor, Ads)
- 🎮 Games (Spin, Dice, Flip, Tap Game)
- 👥 Referral System
- 💸 Withdraw System (Bkash/Nagad)
- 👑 VIP System
- 🛍 Premium Shop
- 🎁 Daily Rewards & Streak
- 📊 Admin Panel
- 🌍 Multi-language (EN/BN)

## Deployment on Render

1. Fork this repository
2. Create a new Web Service on Render
3. Connect your repository
4. Add environment variables:
   - `BOT_TOKEN`: Your bot token from @BotFather
   - `ADMIN_ID`: Your Telegram user ID
5. Deploy!

## Local Development

```bash
pip install -r requirements.txt
export BOT_TOKEN="your_token"
export ADMIN_ID="your_admin_id"
python earnbot.py