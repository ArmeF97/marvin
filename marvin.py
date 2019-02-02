#!/usr/bin/env python3

import json
import logging
import requests
import io
import datetime
import pickle

from threading import Thread
from praw import Reddit, exceptions, models
from lxml.html import fromstring
from urllib import parse as urlparse
from urllib.parse import unquote
from telegram import MessageEntity, ChatMember, Chat
from telegram.ext import MessageHandler, Updater
from telegram import TelegramError
from time import sleep


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
        # Telegram public group's username
        self.tg_group = None
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
        # Groups in which messages come from
        self.tg_groups = {}

    # ---------------------------------------------
    # Util functions
    # ---------------------------------------------

    def get_page_title_from_url(self, page_url: str):
        """
        Function that return the title of the given web page
        :param page_url: The page to get the title from
        :return: A string that contain the title of the given page
        """

        if page_url.startswith("https://www.youtube.com/watch?v="):
            video_id = page_url[32:]
            return self.get_youtube_title_from_url(video_id)
        elif page_url.startswith("https://youtu.be/"):
            video_id = page_url[17:]
            return self.get_youtube_title_from_url(video_id)

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
    def is_sender_admin(bot, chat_id: int, user_id: int):
        """
        Function that return if the given user is an admin in the given chat
        :param bot: The current bot instance
        :param chat_id: The id of the chat
        :param user_id: The id of user to check
        :return: True if the user is an admin in the given chat, False otherwise
        """
        user_info = bot.get_chat_member(chat_id, user_id)
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

    def delete_message_with_delay(self, tg_group_id, message_id, seconds_delay):
        """
        Delete message with delay (no admin check, check before using)
        :param tg_group_id: the id of the group we want to delete the message from
        :param message_id: the id of the message to delete
        :param seconds_delay: delay of the delete (in seconds)
        """
        sleep(seconds_delay)
        self.updater.bot.delete_message(tg_group_id, message_id)
        return

    def delete_message_if_admin(self, tg_group, message_id, seconds_delay=0):
        """
        Delete message by checking if we are admin
        :param tg_group: the group we want to delete the message from
        :param message_id: the id of the message to delete
        :param seconds_delay: delay of the delete (in seconds)
        """

        if tg_group.id not in self.tg_groups:
            self.tg_groups[tg_group.id] = tg_group
            is_admin = self.is_sender_admin(self.updater.bot, tg_group.id, self.updater.bot.id)
            self.tg_groups[tg_group.id].is_admin = is_admin
            if is_admin:
                if seconds_delay > 0:
                    delete_thread = Thread(target=self.delete_message_with_delay,
                                           args=[tg_group.id, message_id, seconds_delay])
                    delete_thread.start()
                else:
                    self.updater.bot.delete_message(tg_group.id, message_id)
        else:
            if self.tg_groups[tg_group.id].is_admin:
                if seconds_delay > 0:
                    delete_thread = Thread(target=self.delete_message_with_delay,
                                           args=[tg_group.id, message_id, seconds_delay])
                    delete_thread.start()
                else:
                    self.updater.bot.delete_message(tg_group.id, message_id)
        return

    def is_message_in_correct_group(self, chat: Chat):
        """
        Function that return if the message has been sent in the correct group
        :param chat: The chat where the message has been sent
        :return: True if the message is in the group saved in the JSON, False otherwise
        """
        return chat.id == self.authorized_group_id

    def add_default_comment(self, post_submission, tg_msg_id):
        """
        Function that add the default comment to the given post submission
        :param post_submission: The submitted post where the bot should add the comment
        :param tg_msg_id: The msg id of the message the original post come from
        """
        string_to_send = self.default_comment_content
        if tg_msg_id is None:
            string_to_send = string_to_send.replace("{TG_MSG_ID}", "")
        else:
            string_to_send = string_to_send.replace("{TG_MSG_ID}", "/" + str(tg_msg_id))
        string_to_send = string_to_send.replace("{SUBREDDIT}", str(self.subreddit))
        string_to_send = string_to_send.replace("{TG_GROUP}", str(self.tg_group))

        comment = post_submission.reply(string_to_send)
        comment.mod.distinguish(sticky=True)
        self.logger.info("Default comment sent!")

    def get_youtube_title_from_url(self, video_id):
        """
        Function that gets title from youtube video
        :param video_id: id of youtube video
        :returns video title
        """

        url_get = "https://youtube.com/get_video_info?video_id=" + video_id

        # http get request to obtain video info
        contents = self.session.get(url_get)
        # contents = urllib.request.urlopen(url_get).read()

        contents = str(contents.text)
        a_point = contents.find("&title=") + 7
        contents = contents[a_point:]
        b_point = contents.find("&")
        contents = contents[:b_point]
        contents = contents.replace("+", " ")
        contents_decoded = unquote(contents)
        return "[YouTube] " + contents_decoded

    def send_tg_message_reply_or_private(self, update, text):
        """ (Telegram command)
        Send a reply in private; when not possible, send in group
        @:param message: an object that represents an incoming message.
        @:param text: text to send
        """
        try:
            self.updater.bot.send_message(update.message.from_user.id, text)
        except TelegramError:
            if update.message.from_user.username is None:
                text_to_send = "[" + str(update.message.from_user.first_name)
                if update.message.from_user.last_name is not None:
                    text_to_send += " " + str(update.message.from_user.last_name)
                text_to_send += ", imposta un username!]" + "\n" + text
            else:
                text_to_send = "@" + str(update.message.from_user.username) + "\n" + text
            self.updater.bot.send_message(chat_id=update.message.chat.id,
                                          text=text_to_send)
        return

    # ---------------------------------------------
    # Bot commands
    # ---------------------------------------------

    def start(self, update):
        """ (Telegram command)
        Send a message when the command /start is issued.
        @:param update: an object that represents an incoming update.
        """
        if update.message.chat.id != self.authorized_group_id:
            update.message.reply_text('Ciao, benvenuto in marvin! Visita la pagina '
                                      'github per maggiori informazioni https://github.com/fen0x/marvin')
        else:
            self.delete_message_if_admin(update.message.chat, update.message.message_id)

        return

    def comment(self, update):
        """ (Telegram command)
        Adds a comment to a reddit post (only if it belong to the authorized subreddit)
        :param update: an object that represents an incoming update.
        """

        # Check if the command has been used in the correct group
        if not self.is_message_in_correct_group(update.message.chat):
            self.delete_message_if_admin(update.message.chat, update.message.message_id)
            self.send_tg_message_reply_or_private(update,
                                                  "Spiacente, questo bot funziona solo nel"
                                                  "gruppo autorizzato con id " +
                                                  str(self.authorized_group_id) +
                                                  ", non in " +
                                                  str(update.message.chat.id))
            return
        # Check if the command is used as reply to another message
        if not update.message.reply_to_message:
            self.delete_message_if_admin(update.message.chat, update.message.message_id)
            self.send_tg_message_reply_or_private(update,
                                                  "Per usare /comment devi rispondere ad un messaggio")
            return
        # Check that the message has the url
        urls_entities = update.message.reply_to_message.parse_entities([MessageEntity.URL])
        if not urls_entities:
            self.delete_message_if_admin(update.message.chat, update.message.message_id)
            self.send_tg_message_reply_or_private(update,
                                                  "Per usare questo comando devi rispondere "
                                                  "ad un messaggio del bot contenente un link")
            return
        # Get the comment content, post id and post the comment
        comment_text = "\\[[Telegram](https://t.me/" + str(self.tg_group) + "/" + str(update.message.message_id) + "/)"
        username = self.get_user_name(update.message)
        comment_text += " - "
        comment_text += "[" + username + "](https://t.me/" + username[1:] + ")" + "\\]  \n"
        comment_text += update.message.text_markdown.replace("/comment", "").strip()
        url = urls_entities.popitem()[1]
        try:
            cutted_url = models.Submission.id_from_url(url)
        except exceptions.ClientException:
            self.delete_message_if_admin(update.message.chat, update.message.message_id)
            self.send_tg_message_reply_or_private(update,
                                                  "Il link a cui hai risposto non è un link di reddit valido")
            return
        submission = self.reddit.submission(id=cutted_url)
        if submission.subreddit.display_name == self.subreddit.display_name:
            if submission.locked:
                self.delete_message_if_admin(update.message.chat, update.message.message_id)
                self.send_tg_message_reply_or_private(update,
                                                      "Non puoi commentare un post lockato!")
                return
            else:
                created_comment = submission.reply(comment_text)
                comment_link = "https://www.reddit.com" + created_comment.permalink
                self.updater.bot.send_message(self.authorized_group_id,
                                              "Commento aggiunto al post! (da: " + self.get_user_name(update.message)
                                              + ")\n" + comment_link,
                                              reply_to_message_id=update.message.reply_to_message.message_id)
                self.logger.info("Comment added to post with id:" + str(cutted_url))
        else:
            self.delete_message_if_admin(update.message.chat, update.message.message_id)
            self.send_tg_message_reply_or_private(update,
                                                  "Non puoi inviare commenti a post"
                                                  "che non appartengono al subreddit: " +
                                                  self.subreddit.display_name)
            return

    def postlink(self, subreddit, update):
        """ (Telegram command)
        Read the link and post it in the subreddit
        :param subreddit: The subreddit where the bot should post the link
        :param update: an object that represents an incoming update.
        """

        # Check if the command has been used in the correct group
        if not self.is_message_in_correct_group(update.message.chat):
            self.delete_message_if_admin(update.message.chat, update.message.message_id)
            self.send_tg_message_reply_or_private(update,
                                                  "Spiacente, questo bot funziona solo nel"
                                                  "gruppo autorizzato con id " +
                                                  str(self.authorized_group_id) +
                                                  ", non in " +
                                                  str(update.message.chat.id))
            return
        # Check if the command is used as reply to another message
        if not update.message.reply_to_message:
            self.delete_message_if_admin(update.message.chat, update.message.message_id)
            self.send_tg_message_reply_or_private(update,
                                                  "Per usare /postlink devi rispondere ad un messaggio")
            return
        # Check if the command has been used from an administrator
        if not self.is_sender_admin(self.updater.bot, update.message.chat.id, update.message.from_user.id):
            self.delete_message_if_admin(update.message.chat, update.message.message_id)
            self.send_tg_message_reply_or_private(update,
                                                  "Spiacente, non sei un amministratore.")
            return
        reply_message = update.message.reply_to_message

        urls_entities = reply_message.parse_entities([MessageEntity.URL])
        if not urls_entities:
            self.delete_message_if_admin(update.message.chat, update.message.message_id)
            self.send_tg_message_reply_or_private(update,
                                                  "Il messaggio originale deve contenere una URL")
            return
        if len(urls_entities) > 1:
            self.delete_message_if_admin(update.message.chat, update.message.message_id)
            self.send_tg_message_reply_or_private(update,
                                                  "Il messaggio originale deve contenere una **sola** URL")
            return

        link_to_post = urls_entities.popitem()[1]
        # Check link schema
        link_parsed = urlparse.urlparse(link_to_post)
        if not link_parsed.scheme:
            link_to_post = 'https://' + link_to_post
        elif link_parsed.scheme not in ['http', 'https']:
            self.delete_message_if_admin(update.message.chat, update.message.message_id)
            self.send_tg_message_reply_or_private(update,
                                                  "Il messaggio originale deve contenere un link HTTP(S)")
            return
        # Fetch page title
        link_page_title = self.get_page_title_from_url(link_to_post)
        if not link_page_title:
            self.delete_message_if_admin(update.message.chat, update.message.message_id)
            self.send_tg_message_reply_or_private(update,
                                                  "Non sono riuscito a trovare il titolo della pagina")
            return
        # Submit to reddit, add the default comment and send the link to Telegram:
        title = "[" + self.title_prefix + self.get_user_name(reply_message) + "] " + link_page_title
        submission = subreddit.submit(title, url=link_to_post)
        self.add_default_comment(submission, update.message.message_id)
        self.updater.bot.send_message(self.authorized_group_id,
                                      "Post creato: " + str(submission.shortlink) +
                                      " (da: " + self.get_user_name(update.message) + ")",
                                      reply_to_message_id=update.message.reply_to_message.message_id)
        self.logger.info("New link-post submitted")

    def posttext(self, subreddit, update):
        """ (Telegram command)
        Given a text and a title (from an admin) it create a text post in the subreddit
        :param subreddit: The subreddit where the bot should post the content
        :param update: an object that represents an incoming update.
        """

        # Check if the command has been used in the correct group
        if not self.is_message_in_correct_group(update.message.chat):
            self.delete_message_if_admin(update.message.chat, update.message.message_id)
            self.send_tg_message_reply_or_private(update,
                                                  "Spiacente, questo bot funziona solo nel"
                                                  "gruppo autorizzato con id " +
                                                  str(self.authorized_group_id) +
                                                  ", non in " +
                                                  str(update.message.chat.id))
            return
        # Check if the command is used as reply to another message
        if not update.message.reply_to_message:
            self.delete_message_if_admin(update.message.chat, update.message.message_id)
            self.send_tg_message_reply_or_private(update,
                                                  "Per usare /posttext devi rispondere ad un messaggio")
            return
        # Check if the command has been used from an administrator
        if not self.is_sender_admin(self.updater.bot, update.message.chat.id, update.message.from_user.id):
            self.delete_message_if_admin(update.message.chat, update.message.message_id)
            self.send_tg_message_reply_or_private(update,
                                                  "Spiacente, non sei un amministratore.")
            return

        reply_message = update.message.reply_to_message

        question_title = "[" + self.title_prefix + self.get_user_name(reply_message) + "] "
        admin_post_title = update.message.text_markdown.replace("/posttext", "").strip()
        if len(admin_post_title) < 1:
            self.delete_message_if_admin(update.message.chat, update.message.message_id)
            self.send_tg_message_reply_or_private(update,
                                                  "Utilizzando il comando, aggiungi "
                                                  "un titolo al post:\n/posttext <titolo>")
            return
        elif len(admin_post_title) < 6:
            self.delete_message_if_admin(update.message.chat, update.message.message_id)
            self.send_tg_message_reply_or_private(update,
                                                  "Serve un titolo più lungo! Riprova")
            return
        else:
            question_title += admin_post_title

        question_content = reply_message.text_markdown

        # Submit to reddit, add the default comment and send the link to Telegram:
        submission = subreddit.submit(question_title, selftext=question_content)
        self.add_default_comment(submission, update.message.message_id)
        self.updater.bot.send_message(self.authorized_group_id,
                                      "Post creato: " + str(submission.shortlink) +
                                      " (da: " + self.get_user_name(update.message) + ")",
                                      reply_to_message_id=update.message.reply_to_message.message_id)
        self.logger.info("New text-post submitted")

    def delrule(self, update):
        """ (Telegram command)
        Delete a post from the subreddit, posting the reason as comment reading it from the rule dictionary
        :param update: update: an object that represents an incoming update.
        """

        # Check if the command has been used in the correct group
        if not self.is_message_in_correct_group(update.message.chat):
            self.delete_message_if_admin(update.message.chat, update.message.message_id)
            self.send_tg_message_reply_or_private(update,
                                                  "Spiacente, questo bot funziona solo nel"
                                                  "gruppo autorizzato con id " +
                                                  str(self.authorized_group_id) +
                                                  ", non in " +
                                                  str(update.message.chat.id))
            return
        # Check if the command is used as reply to another message
        if not update.message.reply_to_message:
            self.delete_message_if_admin(update.message.chat, update.message.message_id)
            self.send_tg_message_reply_or_private(update,
                                                  "Per usare /delrule devi rispondere ad un messaggio")
            return
        # Check if the command has been used from an administrator
        if not self.is_sender_admin(self.updater.bot, update.message.chat.id, update.message.from_user.id):
            self.delete_message_if_admin(update.message.chat, update.message.message_id)
            self.send_tg_message_reply_or_private(update,
                                                  "Spiacente, non sei un amministratore.")
            return
        # Check that the message has the url
        urls_entities = update.message.reply_to_message.parse_entities([MessageEntity.URL])
        if not urls_entities:
            self.delete_message_if_admin(update.message.chat, update.message.message_id)
            self.send_tg_message_reply_or_private(update,
                                                  "Per usare questo comando devi rispondere "
                                                  "ad un messaggio del bot contenente un link")
            return
        # Get the rule content, post the comment and delete the post
        url = urls_entities.popitem()[1]
        try:
            cutted_url = models.Submission.id_from_url(url)
        except exceptions.ClientException:
            self.delete_message_if_admin(update.message.chat, update.message.message_id)
            self.send_tg_message_reply_or_private(update,
                                                  "Il link a cui hai risposto non è un link di reddit valido")
            return
        splitted_message = update.message.text_markdown.replace("/delrule", "").strip().split()
        note_message = None
        rule_text = None
        rule_number = -1
        # Read the rule number
        if len(splitted_message) == 0:
            self.delete_message_if_admin(update.message.chat, update.message.message_id)
            self.send_tg_message_reply_or_private(update,
                                                  "Non hai fornito il numero di regola per rimuovere il post...")
            return
        elif len(splitted_message) >= 1:
            try:
                rule_number = int(splitted_message[0])
            except ValueError:
                self.delete_message_if_admin(update.message.chat, update.message.message_id)
                self.send_tg_message_reply_or_private(update,
                                                      "Hai fornito un numero di regola non valido... "
                                                      "Utilizza il comando con /delrule "
                                                      "<numero regola> <note(opzionale)>")
                return
            if rule_number not in self.rules:
                self.delete_message_if_admin(update.message.chat, update.message.message_id)
                self.send_tg_message_reply_or_private(update,
                                                      "Hai fornito un numero di regola non presente nella lista...")
                return
            rule_text = self.rules[rule_number]
        # Read the note message if present
        if len(splitted_message) > 1:
            note_message = update.message.text_markdown.replace("/delrule", "").replace(str(rule_number), "").strip()
        submission = self.reddit.submission(id=cutted_url)
        if submission.subreddit.display_name == self.subreddit.display_name:
            # Create delete comment
            delete_comment = "Il tuo post è stato rimosso per la violazione del seguente articolo del regolamento:\n\n"
            delete_comment += "* " + rule_text + "\n\n"
            if note_message is not None:
                delete_comment += note_message + "\n\n"
            delete_comment += "Se hai dubbi o domande, ti preghiamo di inviare un messaggio in "
            delete_comment += "[modmail](https://www.reddit.com/message/compose?to=%2Fr%2F" + self.subreddit + ").\n\n"

            # Send the comment, remove and lock the post
            submission.reply(delete_comment)
            mod_object = submission.mod
            mod_object.remove()
            mod_object.lock()
            self.updater.bot.send_message(self.authorized_group_id,
                                          "Il post è stato cancellato! (da: "
                                          + self.get_user_name(update.message) + ")",
                                          reply_to_message_id=update.message.reply_to_message.message_id)
            self.logger.info("Post with id:" + str(cutted_url) + " has been deleted from Telegram")
        else:
            self.delete_message_if_admin(update.message.chat, update.message.message_id)
            self.send_tg_message_reply_or_private(update,
                                                  "Non puoi cancellare post che non appartengono al subreddit: " +
                                                  self.subreddit.display_name)

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
                                   "Postato da: " + submission.author.name + "\n" + \
                                   submission.shortlink
            # Send admin notification
            if self.admin_group_id != 0:
                bot_ref.send_message(self.admin_group_id, notification_content)
            # Send notification to everyone in the authorized group
            if submission.author != self.reddit.user.me().name:
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
        self.logger.warning('\nUpdate status:\n"%s"\nCaused error:\n"%s"', update, error)

    def message_handler(self, bot, update):
        if update.message.text is not None and update.message.text.startswith("/"):
            if update.message.text.startswith("/start"):
                self.start(update)
            elif update.message.text.startswith("/comment"):
                self.comment(update)
            elif update.message.text.startswith("/postlink"):
                self.postlink(self.subreddit, update)
            elif update.message.text.startswith("/posttext"):
                self.posttext(self.subreddit, update)
            elif update.message.text.startswith("/delrule"):
                self.delrule(update)
            else:
                self.delete_message_if_admin(update.message.chat, update.message.message_id, 5)
        return

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
            "User-Agent"] = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 " \
                            "(KHTML, like Gecko) Chrome/71.0.3578.98 Safari/537.36"
        # reddit login
        self.reddit = Reddit(**bot_data_file["reddit"])
        # Read subreddit
        subreddit_name = bot_data_file["reddit"]["subreddit_name"]
        self.subreddit = self.reddit.subreddit(subreddit_name)
        self.logger.info(
            "Connecting to subreddit:" + str(self.subreddit.display_name) + " - " + str(self.subreddit.title))
        # Read authorized group name
        self.authorized_group_id = int(bot_data_file["telegram"]["authorized_group_id"])
        self.admin_group_id = int(bot_data_file["telegram"]["admin_group_id"])
        self.tg_group = bot_data_file["telegram"]["tg_group"]
        # Read the prefix to the post title
        self.title_prefix = bot_data_file["reddit"]["title_prefix"]
        # Create the EventHandler and pass it your bot's token.
        self.logger.info("Starting bot... Logging in...")
        self.updater = Updater(bot_data_file["telegram"]["login_token"])
        self.logger.info("Starting bot... Setting handler...")
        # Get the dispatcher to register handlers
        dp = self.updater.dispatcher

        # Register commands
        dp.add_handler(MessageHandler(filters=None, callback=self.message_handler))

        # log all errors
        dp.add_error_handler(self.error_handler)

        # Start the Bot and the important threads
        self.updater.start_polling()

        new_reddit_posts_thread = Thread(target=self.check_new_reddit_posts, args=[])
        new_reddit_posts_thread.start()

        self.logger.info("Bot successfully loaded...! Bot ready!")

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
