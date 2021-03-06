#!/usr/bin/env python3

import os 
import re
import sys
import time 
import json 
import argparse
import operator
import threading
from slack import RTMClient, WebClient
from datetime import datetime, timedelta

quizbot_rtm = RTMClient(token=os.environ.get("SLACK_BOT_TOKEN"))
quizbot_web = WebClient(token=os.environ.get("SLACK_BOT_TOKEN"))
quizbot_id = None

CHECK_FUNCTIONS = {
    '=': lambda x, y: operator.eq(x.lower(), y.lower())
}

COMMON_INTRO_TEXT = "<!here> Hi there, this is Simon's little baby quizbot"
READY_SET_GO = "Ready? Let's go!"
DEFAULT_HINT_TEXT = "Do you really need a hint??? Keep guessing :)"

class Quiz(object):
    web = None
    rtm = None
    timer = None
    current_question = None
    channel = ''
    intro = ''
    questions = []
    userScores = {}
    botUsers = [] 
    def __init__(self, web, rtm, quiz_file, channel):
        self.web = web
        self.rtm = rtm
        self.channel = self.getChannelID(channel)
        self.intro, self.questions = self.loadQuestions(quiz_file)
        self.botUsers = self.getBots()

    class Question(object):
        text = ''
        answer = ''  # or a list
        hints = []
        time_limit = 180
        time_hint = 60
        check_function = None
        original_score = 1
        score = 1
        def __init__(
            self, question_text, answer, check_function='=', 
            hints=[], score=1, time_hint=60, time_limit=180):
            self.text = question_text
            self.answer = answer
            self.hints = hints
            self.time_limit = time_limit
            self.time_hint = time_hint
            self.original_score = score
            self.score = self.original_score
            # return a function from stringmatch
            self.check_function = self.getCheckFunction(check_function)  
            
        def getCheckFunction(self, check_function):
            return CHECK_FUNCTIONS.get(check_function, operator.eq)

        def checkAnswer(self, answer):
            if isinstance(self.answer, str):
                return self.check_function(self.answer, answer)
            elif isinstance(self.answer, list):
                return any([
                    self.check_function(x, answer) for x in self.answer
                ])

        def decrementScore(self):
            ## take a linear gradient from start to end
            decrementAmount = (self.time_hint / self.time_limit) * self.original_score
            self.score -= decrementAmount

    def sendQuestion(self, text, hint=False):
        if hint is not True:
            text = self.current_question.text

        points = self.current_question.score
        points = round(points, 2)
        # drop decimal if the number is the same
        if points == round(points):
            points = round(points)

        message = f"<!here> *{'Hint' if hint else 'Question'} ({points} point{'s' if points != 1 else ''}):* {text}"
        self.sendString(message)

        if hint is not True:
            self.current_question_start = datetime.now()        
        
        wait_time = self.getWaitTime()
        self.timer = threading.Timer(
            float(wait_time), self.hintOrPass
        )
        self.timer.start()

    def getWaitTime(self):
        time_hint = timedelta(seconds=self.current_question.time_hint)
        time_total = timedelta(seconds=self.current_question.time_limit)
        time_wiggle = timedelta(seconds=5)
        time_start = self.current_question_start
        # add a few seconds to allow some wiggle room
        time_end = time_start + time_total + time_wiggle
        time_now = datetime.now()

        # are we past our finish time
        # we shouldn't get here???
        if time_now > time_end:
            raise Exception('Question should be over!')
        # are we meant to give a hint
        else :
            wait_time = min(
                time_hint.seconds, 
                # remove wiggle time so we hopeully end at correct time
                (time_end - time_now).seconds
            )

            return wait_time

    def hintOrPass(self):
        self.timer.cancel()
        
        time_hint = timedelta(seconds=self.current_question.time_hint)
        time_total = timedelta(seconds=self.current_question.time_limit)
        time_start = self.current_question_start
        time_end = time_start + time_total
        time_now = datetime.now()

        # are we past our finish time
        if time_now > time_end:
            self.endQuestion(fail=True)
        else:
            self.current_question.decrementScore()
            self.sendQuestion(self.getHintText(), hint=True)

    def getHintText(self):
        hints = self.current_question.hints
        if len(hints) > 0:
            text = hints.pop(0)
        else:
            text = DEFAULT_HINT_TEXT
        return text

    def endQuestion(self, fail=False):
        question = self.current_question
        self.timer.cancel()
        if fail is True:
            message = f"No one got it? :(\nThe answer was {self.current_question.answer})"
            self.sendString(message)
            ## sleep 3s to give people a chance
            time.sleep(3)
        if len(self.questions) > 0:
            self.current_question = self.questions.pop(0)
            self.sendQuestion(self.current_question.text)
        else:
            self.current_question = None
            self.end()
        
    def loadQuestions(self, filepath):
        with open(filepath) as f:
            js = json.load(f)
        questions = [self.Question(**x) for x in js['questions']]
        return js['intro_text'], questions

    def getChannelID(self, name):
        public_channels = self.web.channels_list().data['channels']
        private_channels = self.web.groups_list().data['groups']

        channels = public_channels + private_channels
        for channel in channels:
            if channel['name'] == name:
                return channel['id']

    def getBots(self):
        users = self.web.users_list()
        return [user['id'] for user in users if user['is_bot']]

    def sendIntro(self):
        self.sendString(COMMON_INTRO_TEXT)
        time.sleep(3)
        self.sendString(self.intro)
        time.sleep(3)
        self.sendString(READY_SET_GO)

    def sendCorrectMessage(self, user):
        question = self.current_question
        answer = question.answer
        if isinstance(answer, list):
            answerstr = f"\nPossible answers: {answer}"
        elif isinstance(answer, str):
            answerstr = f"{answer}"
        message = f"CORRECT! <@{user}> got the right answer ({answerstr})"
        self.sendString(message)

    def sendIncorrectMessage(self, user):
        question = self.current_question
        message = f"'fraid not, <@{user}>!"
        self.sendString(message)

    def sendScores(self):
        # returns list of tuples ('username', score)
        results = sorted(
            self.userScores.items(), key=operator.itemgetter(1), reverse=True
        )
        # this is a kinda shit way to do it but meh
        topscore = results[0][1]
        bottomscore = results[-1][1]
        winners = [winner for winner in results if winner[1] == topscore]
        if len(winners) == 1:
            winner = results[0]
            message = f"AND THE WINNER IS: <@{winner[0]}> :crown: with a score of {topscore:.2f}!\n"
        elif len(winners) == 0:
            message = f"WTF? Did no-one win? This can't happen...\n"
        else: 
            message = f"We have JOINT winners this week, tied on a score of {topscore:.2f}! They are: \n"
            for winner in winners:
                message += f"\t\t<@{winner[0]}> :crown:\n"
        message += "The results are as follows: \n"
        for i, result in enumerate(results):
            message += f"{i+1}) <@{result[0]}> {':poop:' if result[1] == bottomscore and not bottomscore == topscore else ''} - score {result[1]:.2f}\n"
        self.sendString(message)

    def sayThanks(self):
        message = "Thanks for playing everyone! See you all next week :)"
        self.sendString(message)

    def sendString(self, message):
        time.sleep(0.1)
        self.web.chat_postMessage(
            channel=self.channel,
            text=message,
            run_async=True
        )

    def start(self):
        self.current_question = self.questions.pop(0)
        self.sendQuestion(self.current_question.text)

    def end(self):
        self.current_question = None
        self.sendScores()
        self.sayThanks()
        self.rtm.stop()

    def handleResponse(self, **payload):
        data = payload['data']
        
        # get username
        if 'user' in data.keys():
            user = data['user'] 
        elif 'username' in data.keys():
            user = data['username']
        else:
            return
        
        # ignore all bots, they're cheaters (or me)
        # turns out bot users have a different data struct to other users - cool
        if user in self.botUsers or 'bot_profile' in data.keys():
            return
        
        # make sure it's the right channel
        if data['channel'] != self.channel:
            return
        
        # make sure i'm actually asking a question
        question = self.current_question
        if question is None: 
            return
        
        # parse message
        else:
            old_web = self.web
            self.web = payload['web_client']
            answer = data.get('text', '')
            if question.checkAnswer(answer):
                self.timer.cancel()
                self.sendCorrectMessage(user)
                if user in self.userScores.keys():
                    self.userScores[user] += question.score
                else:
                    self.userScores[user] = question.score 
                self.endQuestion()
                self.web = old_web
            else:
                if not user in self.userScores.keys():
                    self.userScores[user] = 0
                # self.sendIncorrectMessage(user)


def parseCLArgs():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '-f', '--file',
        type=str,
        required=False,
        metavar='./my_quiz.json',
        help='path to quiz file (defaults to quiz.json)',
        default='./quiz.json'
    )
    parser.add_argument(
        '-c', '--channel',
        type=str,
        required=False,
        metavar='Quiz',
        help='Name of the channel to interact with (defaults to quiz)',
        default='Quiz'
    )
    args = parser.parse_args()
    return args


def main():
    args = parseCLArgs()
    quiz = Quiz(
        web=quizbot_web, 
        rtm=quizbot_rtm, 
        quiz_file=args.file, 
        channel=args.channel
    )

    quiz.sendIntro()
    quiz.start()

    @RTMClient.run_on(event='message')
    def handle(**payload):
        quiz.handleResponse(**payload)

    quizbot_rtm.start()


if __name__ == "__main__":
    main()
