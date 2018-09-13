from telegram.ext import Updater, Handler, CommandHandler, MessageHandler, TypeHandler, Filters, BaseFilter
from telegram import MessageEntity, Message
import logging
import praw
import json

# Enable logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)

# Logger rederence
logger = logging.getLogger(__name__)
# Subreddit reference to send posts
subreddit = None


# Define a few command handlers. These usually take the two arguments bot and
# update. Error handlers also receive the raised TelegramError object in error.
def start(bot, update):
    """Send a message when the command /start is issued."""
    update.message.reply_text('Hi!')


def help(bot, update):
    """Send a message when the command /help is issued."""
    update.message.reply_text('Help!')


def postalink(bot, update):
    # print("Reply from:" + str(update.message.reply_to_message.text))
    message_entities_dict = update.message.reply_to_message.parse_entities([MessageEntity.URL])
    print("Size of entities dict:" + str(len(message_entities_dict)))
    if len(message_entities_dict) == 1:
        link_to_post = str(update.message.reply_to_message.parse_entity(next(iter(message_entities_dict))))
        print("Link to post:" + link_to_post)
        update.message.reply_text("Lo posto!")
        ''' This is how to submit to reddit:
        global subreddit
        title = "?"
        subreddit.submit(title, url=link_to_post)
        '''
    else:
        update.message.reply_text("Non posso postare quel contenuto...")


def error(bot, update, error):
    """Log Errors caused by Updates."""
    logger.warning('Update "%s" caused error "%s"', update, error)


def main():
    """Start the bot."""
    print("Starting bot... Reading login Token...")
    # Read the token from the json
    file_name = "bot_data.json"
    bot_data_file = None
    try:
        with open(file_name) as data_file:
            bot_data_file = json.load(data_file)
    except FileNotFoundError:
        print("FATAL ERROR-->" + file_name + " FILE NOT FOUND, ABORTING...")
        quit(1)
    # Create the EventHandler and pass it your bot's token.
    print("Starting bot... Logging in...")
    updater = Updater(bot_data_file["telegram"]["login_token"])
    print("Starting bot... Setting handler...")
    # Get the dispatcher to register handlers
    dp = updater.dispatcher

    # on different commands - answer in Telegram
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("help", help))

    dp.add_handler(CommandHandler("postalink", postalink, Filters.reply))

    # log all errors
    dp.add_error_handler(error)

    # reddit login
    reddit = praw.Reddit(client_id=bot_data_file["reddit"]["client_id"],
                         client_secret=bot_data_file["reddit"]["client_secret"],
                         user_agent=bot_data_file["reddit"]["user_agent"],
                         username=bot_data_file["reddit"]["username"],
                         password=bot_data_file["reddit"]["password"])

    # Read subreddit
    global subreddit
    subreddit = reddit.subreddit(bot_data_file["reddit"]["subreddit_name"])
    # Subreddit test - TODO remove this
    print(subreddit.display_name)
    print(subreddit.title)
    print(subreddit.description)

    # Start the Bot
    updater.start_polling()

    print("Starting bot... Bot ready!")
    # Run the bot until you press Ctrl-C or the process receives SIGINT,
    # SIGTERM or SIGABRT. This should be used most of the time, since
    # start_polling() is non-blocking and will stop the bot gracefully.
    updater.idle()


if __name__ == '__main__':
    main()
