#!/usr/bin/python3

import socket
import configparser
import openai
import time
import textwrap

config = configparser.ConfigParser()
config.read('/path/to/config/config.ini')
openai.api_key = config['openai']['api_key']

server = "irc.newnet.net"
channel = "#channel"
botnick = "Nickname"
nspass = "NICKSERV_PASSWORD"
modeset = "+B"

print(channel)
irc = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
irc.connect((server, 6667))
irc.send(bytes("USER "+ botnick +" "+ botnick +" "+ botnick + " " + botnick + "\n", "UTF-8"))
irc.send(bytes("NICK "+ botnick +"\n", "UTF-8"))
time.sleep(5)
irc.send(bytes("NS IDENTIFY "+ nspass +"\n", "UTF-8"))
time.sleep(5)
irc.send(bytes("MODE "+ botnick +" "+ modeset +"\n", "UTF-8"))
time.sleep(5)
irc.send(bytes("JOIN "+ channel +" "+ server +"\n", "UTF-8"))
response = irc.recv(2040).decode("UTF-8")
if "JOIN " + channel in response:
    print("Joined channel", channel)
else:
    print("Error joining channel", channel)


# Read and discard any messages in the channel for the next 30 seconds
start_time = time.time()
while time.time() - start_time < 15:
    text = irc.recv(2040).decode("UTF-8")


# Now start responding to new messages

previous_messages = []
max_history_length = 10

while True:
    text=irc.recv(2040).decode("UTF-8")
    print(text)

    if text.find('PING') != -1:
        irc.send(bytes('PONG ' + text.split()[1] + '\r\n', "UTF-8"))

    if "PRIVMSG" in text and botnick in text:
        prompt = text.split(botnick)[1].strip()
        previous_messages.append({"role": "system", "content": "You are a IRC chat bot for interacting with users using short answers only"})
        previous_messages.append({"role": "user", "content": prompt})

        if len(previous_messages) > max_history_length:
            previous_messages.pop(0)
        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=previous_messages,
            temperature=0.85,
            max_tokens=150,
            top_p=1,
            frequency_penalty=0.5,
            presence_penalty=1.0,
        )

        print("Sending:", response)
        reply = response['choices'][0]['message']['content']
        print(reply)

        for chunk in textwrap.wrap(reply, 200):
            irc.send(bytes("PRIVMSG "+ channel +" :"+ chunk +"\n", "UTF-8"))
