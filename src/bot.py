import logging
import threading
import time
import datetime
import os
from collections import defaultdict
from functools import wraps
from typing import Callable
from dotenv import load_dotenv

import asyncio
import sentry_sdk
from aiogram import Bot, Dispatcher, executor, types
from aiogram.dispatcher.filters import Command, IDFilter
from aiogram.dispatcher.filters.filters import AndFilter

from src.constants import (
    ADMIN_IDS,  # Нужно будет вставить туда свой id
    COMMAND_HELP,
    COMMAND_ROLL,
    COMMAND_START,
    COMMAND_STATS,
    COMMAND_USER,
    COMMAND_GAME_LEADERS,
    COMMAND_ROUND_LEADERS,
    COMMAND_LAUNCH_SECONDS,
    COMMAND_LAUNCH_TIME,
    COMMAND_QUESTION,
    COMMAND_PRIZE
)
from src.leaderboard import LeaderBoard
from src.utils.logs import async_log_exception, pretty_time_delta
from src.utils.misc import prepare_str

logging.basicConfig(
    level=logging.DEBUG,
)
log = logging.getLogger(__name__)

load_dotenv()
launched = False  # Переменная = запущен ли раунд, не дает сделать roll до раунда
messages = []   # Список хранит id сообщений rollов участников, в целом можно хранить и id
# times = [] - использовался при попытке сделать задание по кнопкам
API_TOKEN = os.getenv('TOKEN')
SUBS_CHAT_ID = os.getenv('CHAT_ID')
duration = 0  # Длина раунда
prizewinners = 5  # Дефолтное значение количества призовых мест


class Manager:
    def __init__(self, token: str, sentry_token: str = None):
        self.bot = Bot(
            token=token,
            timeout=3.0,
        )
        self.dispatcher = Dispatcher(
            bot=self.bot,
        )
        sentry_sdk.init(
            dsn=sentry_token,
            traces_sample_rate=1.0,
        )

        # Game rules
        self.board = LeaderBoard()

        # Runtime stats
        self.counter = 0
        self.unique_chats = set()
        self.started_at = time.time()
        self.func_counter = defaultdict(int)
        self.func_average_resp_time = defaultdict(float)  # milliseconds
        self.func_resp_time = defaultdict(list)  # milliseconds
        self.max_list_size = 1000

    async def on_shutdown(self, dispatcher: Dispatcher):
        log.debug('Dump data')
        self.board.dump_data()

    def run(self):
        self.set_up_commands()

        # self.board.run_update()

        executor.start_polling(
            dispatcher=self.dispatcher,
            skip_updates=True,
            on_shutdown=self.on_shutdown,
        )

    def increment_counter(self, f):
        """Wrap any important function with this."""

        @wraps(f)
        async def inner(message: types.Message, *args, **kwargs):
            fn = f.__name__
            self.counter += 1
            self.func_counter[fn] += 1

            # Calculate response time
            t0 = time.time()
            res = await f(*args, message, **kwargs)
            dt = (time.time() - t0) * 1000
            self.func_average_resp_time[fn] += dt

            # Store only last X values
            self.func_resp_time[fn] = self.func_resp_time[fn][-(self.max_list_size - 1):]
            self.func_resp_time[fn].append(dt)

            chat_id = message.chat.id
            self.unique_chats.add(chat_id)

            return res

        return inner

    def set_up_commands(self):
        self.dispatcher.register_message_handler(
            self.increment_counter(self.show_welcome),
            Command(commands=[COMMAND_START]),
        )
        self.dispatcher.register_message_handler(
            self.increment_counter(self.show_help),
            Command(commands=[COMMAND_HELP]),
        )
        self.dispatcher.register_message_handler(
            self.increment_counter(self.question),
            Command(commands=[COMMAND_QUESTION]),
        )

        # Игра
        self.dispatcher.register_message_handler(
            self.increment_counter(self.roll_once),
            Command(commands=[COMMAND_ROLL]),
        )
        self.dispatcher.register_message_handler(
            self.increment_counter(self.roll_stats_round),
            Command(commands=[COMMAND_ROUND_LEADERS]),
        )
        self.dispatcher.register_message_handler(
            self.increment_counter(self.roll_stats_total),
            Command(commands=[COMMAND_GAME_LEADERS]),
        )

        # Прочие вспомогательные команды, admin only
        self.dispatcher.register_message_handler(
            self.show_user_info,
            AndFilter(
                Command(commands=[COMMAND_USER]),
                IDFilter(chat_id=ADMIN_IDS),
            ),
        )
        self.dispatcher.register_message_handler(
            self.show_stats,
            AndFilter(
                Command(commands=[COMMAND_STATS]),
                IDFilter(chat_id=ADMIN_IDS),
            ),
        )
        self.dispatcher.register_message_handler(
            self.launch_bot_seconds,
            AndFilter(
                Command(commands=[COMMAND_LAUNCH_SECONDS]),
                IDFilter(chat_id=ADMIN_IDS),
            ),
        )
        self.dispatcher.register_message_handler(
            self.launch_bot_time,
            AndFilter(
                Command(commands=[COMMAND_LAUNCH_TIME]),
                IDFilter(chat_id=ADMIN_IDS),
            ),
        )
        self.dispatcher.register_message_handler(
            self.set_prizewinners,
            AndFilter(
                Command(commands=[COMMAND_PRIZE]),
                IDFilter(chat_id=ADMIN_IDS),
            ),
        )
        # self.dispatcher.callback_query_handler()

    @async_log_exception
    async def show_welcome(self, message: types.Message):
        global launched
        launched = False
        user_channel_status = await m.bot.get_chat_member(chat_id=SUBS_CHAT_ID, user_id=message.chat.id)
        print(user_channel_status['status'])  # Проверка на подписку. Можно вынести в функцию и проверять еще раз при броске
        if user_channel_status['status'] != 'left':
            text = [
                'Привет! Это бот для бросания шаров. Каждый пользователь может бросить шары только 1 раз за раунд.',
                'Чем больше *произведение* выпавших кегль - тем лучше.',
                '',
                f'Нажмите /{COMMAND_HELP} чтобы увидеть список доступных комманд.',
                '',
                f'Задайте свой вопрос: /{COMMAND_QUESTION} Ваш вопрос.',
                '',
                f'Или нажмите /{COMMAND_ROLL} чтобы сразу бросить шары.',
            ]
        else:
            text = ["Подпишитесь на канал, чтобы пользоваться ботом!"]
        await message.answer(
            text=prepare_str(text=text),
            parse_mode=types.ParseMode.MARKDOWN,
        )

    @async_log_exception
    async def question(self, message: types.Message):
        question = str(message.get_args())
        await message.answer("Ваш вопрос был передан адмминистратору, ожидайте ответа!", parse_mode=types.ParseMode.MARKDOWN)
        for admin in ADMIN_IDS:
            await m.bot.send_message(chat_id=admin, text=f"Вопрос от {message.chat.id}: {question}")
            # Хз, можно ли по id написать человеку, если нет, надо чуть переделать

    @async_log_exception
    async def roll_once(self, message: types.Message):
        global launched
        chat_id = message.chat.id
        dt = self.board.time_left
        if not launched:
            text = [
                f'*Игра еще не запущена!*',
                '',
            ]
            return await message.answer(
                text=prepare_str(text=text),
                parse_mode=types.ParseMode.MARKDOWN,
            )

        if not self.board.can_add_result(chat_id=chat_id):
            text = [
                f'*Вы уже приняли участие!*',
                '',
            ]
            # Посчитать точное время
            # if dt < 0:
            # Что-то не так с обновлением!
            # msg = 'Вы скоро сможете повторить!'
            # log.error('something wrong with update thread!')
            # else:
            # msg = f'Вы сможете повторить в новом раунде через {pretty_time_delta(dt)}!'
            # text.append(msg)

            return await message.answer(
                text=prepare_str(text=text),
                parse_mode=types.ParseMode.MARKDOWN,
            )
        if dt < 0:
            text = [
                f'*Раунд завершен!*',
                f'К сожалению, Вы не успели принять участие',
                '',
            ]
            return await message.answer(
                text=prepare_str(text=text),
                parse_mode=types.ParseMode.MARKDOWN,
            )
        # Roll
        rolls = [await message.answer_dice(emoji='🎳') for _ in range(3)]

        messages.append(message)

        score = 1
        for v in rolls:
            score *= v["dice"]["value"]

        # Wait for animation
        await asyncio.sleep(3)

        pos = self.board.add_result(
            chat_id=chat_id,
            full_name=message.chat.full_name,
            score=score,
        )

        text = [
            f'Ваш результат: *{score}*',
            f'Прямо сейчас вы на позиции *{pos}*',
            '',
            f'Посмотреть итоги раунда: /{COMMAND_ROUND_LEADERS}',
            # f'Посмотреть лучшие результаты: /{COMMAND_GAME_LEADERS}',
        ]
        await message.answer(
            text=prepare_str(text=text),
            parse_mode=types.ParseMode.MARKDOWN,
        )

    async def abc_roll_stats_round(self, stats_func: Callable, header: str, message: types.Message):
        chat_id = message.chat.id
        stats = stats_func(chat_id=chat_id)
        if not stats:
            return await message.answer(
                text='Пока что ничего нет.',
                parse_mode=types.ParseMode.MARKDOWN,
            )

        text = [
            header,
            '',
        ]

        for pos, item in stats:
            msg_pos = f'*{pos}*' if pos <= 3 else f'{pos}'
            msg = f'{msg_pos}. {item}'
            text.append(msg)

        dt = self.board.time_left
        if dt > 0:
            text.extend([
                '',
                f'Раунд завершится через: {pretty_time_delta(dt)}',
            ])

        await message.answer(
            text=prepare_str(text=text),
            parse_mode=types.ParseMode.MARKDOWN,
        )

    @async_log_exception
    async def roll_stats_round(self, message: types.Message):
        return await self.abc_roll_stats_round(
            stats_func=self.board.current_stats,
            header='*Результаты раунда*',
            message=message,
        )

    @async_log_exception
    async def roll_stats_total(self, message: types.Message):
        return await self.abc_roll_stats_round(
            stats_func=self.board.total_stats,
            header='*Лучшие результаты за сутки*',
            message=message,
        )

    @async_log_exception
    async def show_user_info(self, message: types.Message):
        text = [
            '*Информация об аккаунте*',
            '',
            f'Имя: `{message.chat.full_name}`',
            f'Идентификатор чата: `{message.chat.id}`',
        ]
        await message.answer(
            text=prepare_str(text=text),
            parse_mode=types.ParseMode.MARKDOWN,
        )

    @async_log_exception
    async def show_help(self, message: types.Message):
        text = [
            '*Боулинг*',
            '',
            f'/{COMMAND_ROLL} -- бросить шары.',
            f'/{COMMAND_ROUND_LEADERS} -- итоги раунда.',
            f'/{COMMAND_GAME_LEADERS} -- лучшие результаты.',
            '',
            '*Помощь*',
            '',
            f'/{COMMAND_START} -- запустить бота.',
            f'/{COMMAND_HELP} -- просмотреть это сообщение ещё раз.',
            f'Задайте свой вопрос: /{COMMAND_QUESTION} Ваш вопрос.',
        ]
        if message.chat.id in ADMIN_IDS:
            text.extend([
                '',
                '*Вспомогательные команды*',
                '',
                f'/{COMMAND_LAUNCH_SECONDS} -- запустить раунд на x секунд.',
                f'/{COMMAND_LAUNCH_TIME} -- запустить раунд с HH:MM по HH:MM.',
                f'/{COMMAND_PRIZE} -- Задать количество призовых мест (по умолчанию - 5).',
                f'/{COMMAND_USER} -- посмотреть на себя.',
                f'/{COMMAND_STATS} -- посмотреть статистику бота.',
            ])
        await message.answer(
            text=prepare_str(text=text),
            parse_mode=types.ParseMode.MARKDOWN,
        )

    @async_log_exception
    async def launch_bot_seconds(self, message: types.Message):  # Запуск сразу на n секунд, используется для быстрых тестов
        global duration, launched
        duration = int(message.get_args())
        launched = True
        self.board.run_update(d=duration)
        text = [
            f'Раунд запущен на {duration} секунд!'
        ]
        await message.answer(
            text=prepare_str(text=text),
            parse_mode=types.ParseMode.MARKDOWN,
        )
        await self.time_out_check(d=duration, message=message)

    @async_log_exception
    async def launch_bot_time(self, message: types.Message):  # Запуск с XX:YY по ZZ:AA текущего дня
        global duration, launched
        current_time = datetime.datetime.now()
        t = str(message.get_args())
        start_hour = int(t[:2])
        start_minute = int(t[3:5])
        stop_hour = int(t[6:8])
        stop_minute = int(t[9:11])
        start_time = datetime.datetime(datetime.datetime.now().year, datetime.datetime.now().month,
                                       datetime.datetime.now().day, start_hour, start_minute)
        stop_time = datetime.datetime(datetime.datetime.now().year, datetime.datetime.now().month,
                                      datetime.datetime.now().day,  stop_hour, stop_minute)
        delta = (start_time - current_time).seconds
        await asyncio.sleep(delta)
        launched = True
        duration = (stop_time - start_time).seconds
        self.board.run_update(d=duration)
        text = [
            f'Раунд запущен с {start_time} по {stop_time}'
        ]
        await message.answer(
            text=prepare_str(text=text),
            parse_mode=types.ParseMode.MARKDOWN,
        )
        await self.time_out_check(d=duration, message=message)

#    Попытка сделать кнопки
#    @async_log_exception
#    async def launch_bot_timestart(self, message: types.Message):
#        current_hour = datetime.datetime.now().time().hour
#        keyboard = types.InlineKeyboardMarkup()
#        for i in range(8):
#            times.append((current_hour + 1 + i) % 24)
#            keyboard.add(types.InlineKeyboardButton(text=f"{times[i]}:00", callback_data=f"button{i + 1}"))
#        await message.answer("Выберите время начала раунда", reply_markup=keyboard)
    @async_log_exception
    async def time_out_check(self, d, message: types.Message):
        await asyncio.sleep(d)
        text = [
            "Раунд окончен!",

            "Спасибо за участие"
        ]
        for msg in messages:  # Перебираем всех участников раунда
            await m.bot.send_message(chat_id=msg.chat.id, text=prepare_str(text=text))
            await self.roll_stats_round(message=msg)
        await self.przies()

    @async_log_exception
    async def set_prizewinners(self, message: types.Message):
        global prizewinners
        prizewinners = int(message.get_args())
        text = [
            f'Количество призовых мест: {prizewinners}'
        ]
        await message.answer(
            text=prepare_str(text=text),
            parse_mode=types.ParseMode.MARKDOWN,
        )

    @async_log_exception
    async def przies(self):
        leaders = self.board.get_leads()[:prizewinners]
        for i in range(len(leaders)):
            text = [
                f"Поздравляем! Вы заняли {i + 1} место!",

                "Для получения приза обратитесть к администратору сообщества."
            ]
            await m.bot.send_message(chat_id=leaders[i].chat_id, text=prepare_str(text=text))

    @async_log_exception
    async def show_stats(self, message: types.Message):
        if not self.func_counter:
            return await message.answer(
                text='Сейчас тут ничего нет.',
            )

        now = time.time()
        lifetime = pretty_time_delta(now - self.started_at)

        text = [
            '*Статистика бота*',
            '',
            f'- Всего запросов с момента старта: *{self.counter}*',
            f'- Всего пользователей с момента старта: *{len(self.unique_chats)}*',
            f'- Время жизни бота: {lifetime}',
            '',
            '*Статистика по функциям*',
            '',
        ]
        total_resp_time = []
        sorted_requests = sorted(self.func_counter.items(), key=lambda i: (i[1], i[0]), reverse=True)
        for (fn, requests) in sorted_requests:
            # AVG resp time
            resp_time = self.func_resp_time[fn]
            total_resp_time.extend(resp_time)
            avg_resp = sum(resp_time) / len(resp_time)

            text.append(f'`{fn}`')
            text.append(f'{requests} requests, {avg_resp:.0f} avg resp time (ms)')
            text.append('')

        # Вставить после ``Всего запросов..``
        total_avg = sum(total_resp_time) / len(total_resp_time)
        text.insert(3, f'- Среднее время ответа: *{total_avg:.0f}* (ms)')

        await message.answer(
            text=prepare_str(text=text),
            parse_mode=types.ParseMode.MARKDOWN,
        )


if __name__ == '__main__':
    # TG_TOKEN = os.getenv('TG_TOKEN')
    TG_TOKEN = API_TOKEN
    assert TG_TOKEN, 'TG_TOKEN env variable must be set!'

    SENTRY_TOKEN = os.getenv('SENTRY_TOKEN')

    m = Manager(
        token=TG_TOKEN,
    )
    m.run()
