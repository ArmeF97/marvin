#!/usr/bin/env python3

import json
import logging
import requests
import praw
import io
import datetime
import pickle

from threading import Thread
from lxml.html import fromstring
from urllib import parse as urlparse
from functools import partial
from telegram import MessageEntity, ChatMember, User, Chat
from telegram.ext import CommandHandler, Filters, Updater


class MarvinBot:
    # The files to open on startup
    config_file_name = "content/bot_data.json"
    comment_file_name = "content/defaultComment.txt"
    rules_file_name = "content/delete_post_rules.json"
    cookie_cache_file_name = "content/cookies.pkl"

    def __init__(self, logger_ref):
        # The subreddit where the bot must post
        self.subreddit = None
        # The authorized group id, used to deny commands from other chats (From JSON)
        self.authorized_group_id = None
        # The admin group id, used to send all new post notification to them (From JSON)
        self.admin_group_id = None
        # The default comment the bot will automatically add to every post submitted (From txt)
        self.default_comment_content = None
        # The title prefix to use when submitting a post (From JSON)
        self.title_prefix = None
        # Reference to the reddit instance
        self.reddit = None
        # Dictionary used to contain all the rules used when deleting a post
        self.rules = {}
        # Logger Reference
        self.logger = logger_ref
        # Requests session
        self.session = None
        # Telegram Updater - telegram.ext.Updater
        self.updater = None
        # List used to avoid notification on telegram created posts
        self.created_posts = []

    # ---------------------------------------------
    # Util functions
    # ---------------------------------------------

    def get_page_title_from_url(self, page_url: str):
        """
        Function that return the title of the given web page
        :param page_url: The page to get the title from
        :return: A string that contain the title of the given page
        """
        r = self.session.get(page_url)

        # Update cookie cache:
        try:
            with open(self.cookie_cache_file_name, "wb") as f:
                pickle.dump(self.session.cookies, f)
        except Exception as e:
            self.logger.warning("Unable to update cached cookies!", exc_info=e)

        tree = fromstring(r.content)
        title = tree.findtext('.//title')
        if title is not None:
            return str(title)
        else:
            return None

    @staticmethod
    def is_sender_admin(bot, chat_id: int, user: User):
        """
        Function that return if the given user is an admin in the given chat
        :param bot: The current bot instance
        :param chat_id: The id of the chat
        :param user: The user to check
        :return: True if the user is an admin in the given chat, False otherwise
        """
        user_info = bot.get_chat_member(chat_id, user)
        if user_info.status == ChatMember.ADMINISTRATOR or user_info.status == ChatMember.CREATOR:
            return True
        else:
            return False

    @staticmethod
    def get_user_name(message):
        """
        Get the best user name from Telegram
        :param message: the message
        :return: The user nickname when available, the full name otherwise
        """
        user = message.from_user
        if user.username is not None:
            return '@' + user.username
        else:
            return user.full_name

    def is_message_in_correct_group(self, chat: Chat):
        """
        Function that return if the message has been sent in the correct group
        :param chat: The chat where the message has been sent
        :return: True if the message is in the group saved in the JSON, False otherwise
        """
        return chat.id == self.authorized_group_id

    def add_default_comment(self, post_submission):
        """
        Function that add the default comment to the given post submission
        :param post_submission: The submitted post where the bot should add the comment
        """
        comment = post_submission.reply(self.default_comment_content)
        comment.mod.distinguish(sticky=True)
        self.logger.info("Default comment sent!")

    # ---------------------------------------------
    # Bot commands
    # ---------------------------------------------

    def start(self, bot, update):
        """ (Telegram command)
        Send a message when the command /start is issued.
        @:param bot: an object that represents a Telegram Bot.
        @:param update: an object that represents an incoming update.
        """
        update.message.reply_text('In un gruppo, rispondi ad un link con il comando /postlink')

    def comment(self, bot, update):
        """ (Telegram command)
        Adds a comment to a reddit post (only if it belong to the authorized subreddit)
        :param bot: an object that represents a Telegram Bot.
        :param update: an object that represents an incoming update.
        """
        # Check if the command is used as reply to another message
        if not update.message.reply_to_message:
            update.message.reply_text("Per usare questo comando devi rispondere ad un messaggio")
            return
        # Check if the command has been used in the correct group
        if not self.is_message_in_correct_group(update.message.chat):
            update.message.reply_text("Spiacente, questo bot funziona solo nel gruppo autorizzato")
            return
        # Check that the message has the url
        urls_entities = update.message.reply_to_message.parse_entities([MessageEntity.URL])
        if not urls_entities:
            update.message.reply_text(
                "Per usare questo comando devi rispondere ad un messaggio del bot contenente un link")
            return
        # Get the comment content, post id and post the comment
        comment_text = "\\[[Telegram](https://t.me/ItalyInformatica/" + str(update.message.message_id) + "/)"
        username = self.get_user_name(update.message)
        comment_text += " - "
        comment_text += "[" + username + "](https://t.me/" + username[1:] + ")" + "\\]  \n"
        comment_text += update.message.text_markdown.replace("/comment", "").strip()
        url = urls_entities.popitem()[1]
        try:
            cutted_url = praw.models.Submission.id_from_url(url)
        except praw.exceptions.ClientException:
            update.message.reply_text(
                "Il link a cui hai risposto non è un link di reddit valido")
            return
        submission = self.reddit.submission(id=cutted_url)
        if submission.subreddit.display_name == self.subreddit.display_name:
            if submission.locked:
                update.message.reply_text(
                    "Non puoi commentare un post lockato!")
            else:
                created_comment = submission.reply(comment_text)
                comment_link = "https://www.reddit.com" + created_comment.permalink
                update.message.reply_text(
                    "Commento aggiunto al post! (da:" + self.get_user_name(
                        update.message) + ")\n" + comment_link)
                self.logger.info("Comment added to post with id:" + str(cutted_url))
        else:
            update.message.reply_text(
                "Non puoi inviare commenti a post che non appartengono al subreddit: " + self.subreddit.display_name)
            return

    def postlink(self, subreddit, bot, update):
        """ (Telegram command)
        Read the link and post it in the subreddit
        :param subreddit: The subreddit where the bot should post the link
        :param bot: an object that represents a Telegram Bot.
        :param update: an object that represents an incoming update.
        """
        # Check if the command is used as reply to another message
        if not update.message.reply_to_message:
            update.message.reply_text("Per usare questo comando devi rispondere ad un messaggio")
            return
        # Check if the command has been used in the correct group
        if not self.is_message_in_correct_group(update.message.chat):
            update.message.reply_text("Spiacente, questo bot funziona solo nel gruppo autorizzato")
            return
        # Check if the command has been used from an administrator
        if not self.is_sender_admin(bot, update.message.chat.id, update.message.from_user.id):
            update.message.reply_text("Spiacente, non sei un amministratore.")
            return
        reply_message = update.message.reply_to_message

        urls_entities = reply_message.parse_entities([MessageEntity.URL])
        if not urls_entities:
            update.message.reply_text("Il messaggio originale deve contenere una URL")
            return
        if len(urls_entities) > 1:
            update.message.reply_text("Il messaggio originale deve contenere una **sola** URL")
            return

        link_to_post = urls_entities.popitem()[1]
        # Check link schema
        link_parsed = urlparse.urlparse(link_to_post)
        if not link_parsed.scheme:
            link_to_post = 'https://' + link_to_post
        elif link_parsed.scheme not in ['http', 'https']:
            update.message.reply_text("Il messaggio originale deve contenere un link HTTP(S)")
            return
        # Fetch page title
        link_page_title = self.get_page_title_from_url(link_to_post)
        if not link_page_title:
            update.message.reply_text("Non sono riuscito a trovare il titolo della pagina")
            return
        # Submit to reddit, add the default comment and send the link to Telegram:
        title = "[" + self.title_prefix + self.get_user_name(reply_message) + "] " + link_page_title
        submission = subreddit.submit(title, url=link_to_post)
        self.created_posts.append(submission.id)
        self.add_default_comment(submission)
        update.message.reply_text(
            "Post creato: " + str(submission.shortlink) + " (da:" + self.get_user_name(update.message) + ")")
        self.logger.info("New link-post submitted")

    def posttext(self, subreddit, bot, update):
        """ (Telegram command)
        Given a text and a title (from an admin) it create a text post in the subreddit
        :param subreddit: The subreddit where the bot should post the content
        :param bot: an object that represents a Telegram Bot.
        :param update: an object that represents an incoming update.
        """
        # Check if the command is used as reply to another message
        if not update.message.reply_to_message:
            update.message.reply_text("Per usare questo comando devi rispondere ad un messaggio")
            return
        # Check if the command has been used in the correct group
        if not self.is_message_in_correct_group(update.message.chat):
            update.message.reply_text("Spiacente, questo bot funziona solo nel gruppo autorizzato")
            return
        # Check if the command has been used from an administrator
        if not self.is_sender_admin(bot, update.message.chat.id, update.message.from_user.id):
            update.message.reply_text("Spiacente, non sei un amministratore.")
            return
        reply_message = update.message.reply_to_message

        question_title = "[" + self.title_prefix + self.get_user_name(reply_message) + "] "
        admin_post_title = update.message.text_markdown.replace("/posttext", "").strip()
        if len(admin_post_title) < 5:
            update.message.reply_text("Utilizzando il comando, aggiungi un titolo al post:\n/posttext <titolo>")
            return
        else:
            question_title += admin_post_title

        question_content = reply_message.text_markdown

        # Submit to reddit, add the default comment and send the link to Telegram:
        submission = subreddit.submit(question_title, selftext=question_content)
        self.created_posts.append(submission.id)
        self.add_default_comment(submission)
        update.message.reply_text(
            "Post creato: " + str(submission.shortlink) + " (da:" + self.get_user_name(update.message) + ")")
        self.logger.info("New text-post submitted")

    def delrule(self, bot, update):
        """ (Telegram command)
        Delete a post from the subreddit, posting the reason as comment reading it from the rule dictionary
        :param bot:  bot: an object that represents a Telegram Bot.
        :param update: update: an object that represents an incoming update.
        """
        # Check if the command is used as reply to another message
        if not update.message.reply_to_message:
            update.message.reply_text("Per usare questo comando devi rispondere ad un messaggio")
            return
        # Check if the command has been used in the correct group
        if not self.is_message_in_correct_group(update.message.chat):
            update.message.reply_text("Spiacente, questo bot funziona solo nel gruppo autorizzato")
            return
        # Check if the command has been used from an administrator
        if not self.is_sender_admin(bot, update.message.chat.id, update.message.from_user.id):
            update.message.reply_text("Spiacente, non sei un amministratore.")
            return
        # Check that the message has the url
        urls_entities = update.message.reply_to_message.parse_entities([MessageEntity.URL])
        if not urls_entities:
            update.message.reply_text(
                "Per usare questo comando devi rispondere ad un messaggio del bot contenente un link")
            return
        # Get the rule content, post the comment and delete the post
        url = urls_entities.popitem()[1]
        try:
            cutted_url = praw.models.Submission.id_from_url(url)
        except praw.exceptions.ClientException:
            update.message.reply_text(
                "Il link a cui hai risposto non è un link di reddit valido")
            return
        splitted_message = update.message.text_markdown.replace("/delrule", "").strip().split()
        note_message = None
        rule_text = None
        rule_number = -1
        # Read the rule number
        if len(splitted_message) == 0:
            update.message.reply_text(
                "Non hai fornito il numero di regola per rimuovere il post...")
            return
        elif len(splitted_message) >= 1:
            try:
                rule_number = int(splitted_message[0])
            except ValueError:
                update.message.reply_text(
                    "Hai fornito un numero di regola non valido... Utilizza il comando con /delrule <numero regola> <note(opzionale>")
                return
            if rule_number not in self.rules:
                update.message.reply_text(
                    "Hai fornito un numero di regola non presente nella lista...")
                return
            rule_text = self.rules[rule_number]
        # Read the note message if present
        if len(splitted_message) > 1:
            note_message = update.message.text_markdown.replace("/delrule", "").replace(str(rule_number), "").strip()
        submission = self.reddit.submission(id=cutted_url)
        if submission.subreddit.display_name == self.subreddit.display_name:
            # Create delete comment
            delete_coment = "Il tuo post è stato rimosso per la violazione del seguente articolo del regolamento:\n\n"
            delete_coment += "* " + rule_text + "\n\n"
            if note_message is not None:
                delete_coment += note_message + "\n\n"
            delete_coment += "Se hai dubbi o domande, ti preghiamo di inviare un messaggio in "
            delete_coment += "[modmail](https://www.reddit.com/message/compose?to=%2Fr%2FItalyInformatica).\n\n"

            # Send the comment, remove and lock the post
            submission.reply(delete_coment)
            mod_object = submission.mod
            mod_object.remove()
            mod_object.lock()
            update.message.reply_text(
                "Il post è stato cancellato! (da:" + self.get_user_name(update.message) + ")")
            self.logger.info("Post with id:" + str(cutted_url) + " has been deleted from Telegram")
        else:
            update.message.reply_text(
                "Non puoi cancellare post che non appartengono al subreddit: " + self.subreddit.display_name)
            return

    # ---------------------------------------------
    # Threads
    # ---------------------------------------------

    def check_new_reddit_posts(self):
        """
        This function listen for new post being submitted in the connected subreddit
        When a new post appear, it send a Telegram message in the authorized group
        """
        bot_ref = self.updater.bot
        self.logger.info("check_new_reddit_posts thread started")
        for submission in self.subreddit.stream.submissions(skip_existing=True):
            notification_content = submission.title + "\n" + \
                                   "Postato da:" + submission.author.name + "\n" + \
                                   submission.shortlink
            # Send admin notification
            bot_ref.send_message(self.admin_group_id, notification_content)
            # Send notification to everyone in the authorized group
            if submission.id in self.created_posts:
                self.created_posts.remove(submission.id)
            else:
                bot_ref.send_message(self.authorized_group_id, submission.title + "\n" + submission.shortlink)

    # ---------------------------------------------
    # Bot Start and Error manager
    # ---------------------------------------------

    def error_handler(self, bot, update, error):
        """
        Log Errors caused by telegram Updates.
        :param bot: an object that represents a Telegram Bot.
        :param update: an object that represents an incoming update.
        :param error: an object that represents Telegram errors.
        """
        self.logger.warning('Update "%s" caused error "%s"', update, error)

    def main(self):
        """Start the bot."""
        self.logger.info("Starting bot... Reading login Token...")
        # Read the token from the json
        bot_data_file = None
        try:
            with open(self.config_file_name) as data_file:
                bot_data_file = json.load(data_file)
        except FileNotFoundError:
            self.logger.error("FATAL ERROR-->" + self.config_file_name + " FILE NOT FOUND, ABORTING...")
            quit(1)
        # Read the default comment data
        try:
            file = io.open(self.comment_file_name, mode="r", encoding="utf-8")
            self.default_comment_content = file.read()
            file.close()
        except FileNotFoundError:
            self.logger.error("FATAL ERROR-->" + self.comment_file_name + " FILE NOT FOUND, ABORTING...")
            quit(1)
        # Read the rules used to delete a post
        try:
            with open(self.rules_file_name) as data_file:
                rules_list = json.load(data_file)
                for current_rule in rules_list["rules"]:
                    self.rules[current_rule["number"]] = current_rule["text"]
        except FileNotFoundError:
            self.logger.error("FATAL ERROR-->" + self.config_file_name + " FILE NOT FOUND, ABORTING...")
            quit(1)

        # Setup requests session:
        self.session = requests.Session()

        # Load cached cookies
        try:
            with open(self.cookie_cache_file_name, "rb") as f:
                self.session.cookies.update(pickle.load(f))
        except FileNotFoundError:
            self.logger.info("Unable to load cached cookies, creating new ones automatically.")

        # Set custom UserAgent:
        self.session.headers[
            "User-Agent"] = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/71.0.3578.98 Safari/537.36"
        # reddit login
        self.reddit = praw.Reddit(**bot_data_file["reddit"])
        # Read subreddit
        subreddit_name = bot_data_file["reddit"]["subreddit_name"]
        self.subreddit = self.reddit.subreddit(subreddit_name)
        self.logger.info(
            "Connecting to subreddit:" + str(self.subreddit.display_name) + " - " + str(self.subreddit.title))
        # Read authorized group name
        self.authorized_group_id = int(bot_data_file["telegram"]["authorized_group_id"])
        self.admin_group_id = int(bot_data_file["telegram"]["admin_group_id"])
        # Read the prefix to the post title
        self.title_prefix = bot_data_file["reddit"]["title_prefix"]
        # Create the EventHandler and pass it your bot's token.
        self.logger.info("Starting bot... Logging in...")
        self.updater = Updater(bot_data_file["telegram"]["login_token"])
        self.logger.info("Starting bot... Setting handler...")
        # Get the dispatcher to register handlers
        dp = self.updater.dispatcher

        # Register commands
        dp.add_handler(CommandHandler("start", self.start))

        dp.add_handler(CommandHandler("postlink", partial(self.postlink, self.subreddit), Filters.reply))

        dp.add_handler(CommandHandler("posttext", partial(self.posttext, self.subreddit), Filters.reply))

        dp.add_handler(CommandHandler("delrule", self.delrule, Filters.reply))

        dp.add_handler(CommandHandler("comment", self.comment, Filters.reply))

        # log all errors
        dp.add_error_handler(self.error_handler)

        # Start the Bot and the important threads
        self.updater.start_polling()

        new_reddit_posts_thread = Thread(target=self.check_new_reddit_posts, args=[])
        new_reddit_posts_thread.start()

        self.logger.info("Starting bot... Bot ready!")

        self.updater.idle()


if __name__ == '__main__':
    # Enable logging creating logger and file handler
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
    logger = logging.getLogger(__name__)

    now = datetime.datetime.now()
    filename = str(now.year) + "-" + str(now.month) + "-" + str(now.day) + "-" + str(now.hour) + "-" + str(
        now.minute) + "-" + str(now.second)

    fh = logging.FileHandler('logs/' + filename + '.log')
    fh.setLevel(logging.INFO)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    # Create and start the bot class
    MarvinBot(logger).main()
