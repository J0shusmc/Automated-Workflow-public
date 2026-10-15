from __future__ import annotations

import curses
import random
import time
from dataclasses import dataclass
from pathlib import Path


BOARD_WIDTH = 40
BOARD_HEIGHT = 22
INITIAL_SPEED = 8.0
MAX_SPEED = 18.0
HIGH_SCORE_FILE = Path(__file__).with_name("high_score.txt")


@dataclass(frozen=True)
class Point:
    x: int
    y: int

    def __add__(self, other: "Point") -> "Point":
        return Point(self.x + other.x, self.y + other.y)


UP = Point(0, -1)
DOWN = Point(0, 1)
LEFT = Point(-1, 0)
RIGHT = Point(1, 0)

KEY_DIRECTIONS = {
    curses.KEY_UP: UP,
    ord("w"): UP,
    ord("W"): UP,
    curses.KEY_DOWN: DOWN,
    ord("s"): DOWN,
    ord("S"): DOWN,
    curses.KEY_LEFT: LEFT,
    ord("a"): LEFT,
    ord("A"): LEFT,
    curses.KEY_RIGHT: RIGHT,
    ord("d"): RIGHT,
    ord("D"): RIGHT,
}


def load_high_score() -> int:
    try:
        return int(HIGH_SCORE_FILE.read_text(encoding="utf-8").strip() or "0")
    except (FileNotFoundError, ValueError):
        return 0


def save_high_score(score: int) -> None:
    current = load_high_score()
    if score > current:
        HIGH_SCORE_FILE.write_text(f"{score}\n", encoding="utf-8")


def center_text(window: curses.window, y: int, text: str, attr: int = 0) -> None:
    height, width = window.getmaxyx()
    if 0 <= y < height:
        window.addstr(y, max(0, (width - len(text)) // 2), text[: max(0, width - 1)], attr)


def random_food(snake: list[Point]) -> Point:
    occupied = set(snake)
    available = [
        Point(x, y)
        for y in range(BOARD_HEIGHT)
        for x in range(BOARD_WIDTH)
        if Point(x, y) not in occupied
    ]
    return random.choice(available)


def draw_border(window: curses.window, top: int, left: int) -> None:
    horizontal = "#" * (BOARD_WIDTH + 2)
    window.addstr(top, left, horizontal)
    for row in range(BOARD_HEIGHT):
        window.addstr(top + row + 1, left, "#")
        window.addstr(top + row + 1, left + BOARD_WIDTH + 1, "#")
    window.addstr(top + BOARD_HEIGHT + 1, left, horizontal)


def draw_game(
    window: curses.window,
    snake: list[Point],
    food: Point,
    direction: Point,
    score: int,
    high_score: int,
    paused: bool,
) -> None:
    window.erase()
    height, width = window.getmaxyx()
    board_total_width = BOARD_WIDTH + 2
    board_total_height = BOARD_HEIGHT + 2
    top = max(0, (height - board_total_height) // 2)
    left = max(0, (width - board_total_width) // 2)

    title = f"Snake  Score: {score}  Best: {max(score, high_score)}"
    center_text(window, max(0, top - 2), title, curses.A_BOLD)
    center_text(window, max(0, top - 1), "Arrows/WASD move | P pause | Q quit")

    draw_border(window, top, left)
    for index, point in enumerate(snake):
        char = "@" if index == 0 else "O"
        window.addstr(top + point.y + 1, left + point.x + 1, char, curses.A_BOLD)
    window.addstr(top + food.y + 1, left + food.x + 1, "*", curses.A_BOLD)

    if paused:
        center_text(window, top + BOARD_HEIGHT // 2, "PAUSED", curses.A_REVERSE | curses.A_BOLD)

    arrow = {
        UP: "^",
        DOWN: "v",
        LEFT: "<",
        RIGHT: ">",
    }[direction]
    center_text(window, min(height - 1, top + BOARD_HEIGHT + 3), f"Direction: {arrow}")
    window.refresh()


def opposite(a: Point, b: Point) -> bool:
    return a.x + b.x == 0 and a.y + b.y == 0


def screen_too_small(window: curses.window) -> bool:
    height, width = window.getmaxyx()
    return height < BOARD_HEIGHT + 5 or width < BOARD_WIDTH + 4


def show_message(window: curses.window, title: str, subtitle: str) -> int:
    window.nodelay(False)
    while True:
        window.erase()
        center_text(window, 3, title, curses.A_BOLD)
        center_text(window, 5, subtitle)
        center_text(window, 7, "Press Enter to play, or Q to quit.")
        window.refresh()
        key = window.getch()
        if key in (ord("\n"), ord("\r"), curses.KEY_ENTER):
            window.nodelay(True)
            return key
        if key in (ord("q"), ord("Q"), 27):
            return key


def play_round(window: curses.window, high_score: int) -> int | None:
    snake = [
        Point(BOARD_WIDTH // 2, BOARD_HEIGHT // 2),
        Point(BOARD_WIDTH // 2 - 1, BOARD_HEIGHT // 2),
        Point(BOARD_WIDTH // 2 - 2, BOARD_HEIGHT // 2),
    ]
    direction = RIGHT
    pending_direction = RIGHT
    food = random_food(snake)
    score = 0
    paused = False
    last_tick = time.monotonic()

    window.nodelay(True)
    while True:
        if screen_too_small(window):
            window.erase()
            center_text(window, 3, "Terminal window is too small.", curses.A_BOLD)
            center_text(window, 5, f"Resize to at least {BOARD_WIDTH + 4}x{BOARD_HEIGHT + 5}.")
            center_text(window, 7, "Press Q to quit.")
            window.refresh()
            key = window.getch()
            if key in (ord("q"), ord("Q"), 27):
                return None
            time.sleep(0.1)
            continue

        key = window.getch()
        while key != -1:
            if key in (ord("q"), ord("Q"), 27):
                return None
            if key in (ord("p"), ord("P"), ord(" ")):
                paused = not paused
            if key in KEY_DIRECTIONS:
                next_direction = KEY_DIRECTIONS[key]
                if not opposite(next_direction, direction):
                    pending_direction = next_direction
            key = window.getch()

        speed = min(MAX_SPEED, INITIAL_SPEED + score * 0.25)
        if not paused and time.monotonic() - last_tick >= 1.0 / speed:
            direction = pending_direction
            new_head = snake[0] + direction

            hit_wall = (
                new_head.x < 0
                or new_head.x >= BOARD_WIDTH
                or new_head.y < 0
                or new_head.y >= BOARD_HEIGHT
            )
            hit_self = new_head in snake
            if hit_wall or hit_self:
                return score

            snake.insert(0, new_head)
            if new_head == food:
                score += 1
                if len(snake) == BOARD_WIDTH * BOARD_HEIGHT:
                    return score
                food = random_food(snake)
            else:
                snake.pop()

            last_tick = time.monotonic()

        draw_game(window, snake, food, direction, score, high_score, paused)
        time.sleep(0.01)


def run(window: curses.window) -> None:
    curses.curs_set(0)
    window.keypad(True)
    curses.use_default_colors()
    random.seed()

    high_score = load_high_score()
    key = show_message(window, "SNAKE", "Eat food, grow longer, and avoid walls.")
    if key in (ord("q"), ord("Q"), 27):
        return

    while True:
        score = play_round(window, high_score)
        if score is None:
            return

        save_high_score(score)
        high_score = max(high_score, score)
        key = show_message(window, "GAME OVER", f"Final score: {score} | Best: {high_score}")
        if key in (ord("q"), ord("Q"), 27):
            return


def main() -> None:
    curses.wrapper(run)


if __name__ == "__main__":
    main()
