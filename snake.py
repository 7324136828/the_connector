import pygame
import random
import sys

pygame.init()

GRID_SIZE = 20
GRID_WIDTH = 30
GRID_HEIGHT = 20
CELL_SIZE = 20

BLACK = (0, 0, 0)
GREEN = (0, 255, 0)
DARK_GREEN = (0, 200, 0)
RED = (255, 0, 0)
WHITE = (255, 255, 255)

UP = (0, -1)
DOWN = (0, 1)
LEFT = (-1, 0)
RIGHT = (1, 0)

class Snake:
    def __init__(self):
        self.reset()

    def reset(self):
        start_x = GRID_WIDTH // 2
        start_y = GRID_HEIGHT // 2
        self.body = [(start_x, start_y), (start_x - 1, start_y), (start_x - 2, start_y)]
        self.direction = RIGHT
        self.grow = False

    def set_direction(self, direction):
        if (direction[0] * -1, direction[1] * -1) != self.direction:
            self.direction = direction

    def move(self):
        head_x, head_y = self.body[0]
        dir_x, dir_y = self.direction
        new_head = (head_x + dir_x, head_y + dir_y)
        self.body.insert(0, new_head)
        if not self.grow:
            self.body.pop()
        else:
            self.grow = False

    def check_collision(self):
        head = self.body[0]
        if head[0] < 0 or head[0] >= GRID_WIDTH or head[1] < 0 or head[1] >= GRID_HEIGHT:
            return True
        if head in self.body[1:]:
            return True
        return False

    def eat(self):
        self.grow = True

class Food:
    def __init__(self):
        self.position = (0, 0)
        self.randomize()

    def randomize(self, snake_body=None):
        while True:
            x = random.randint(0, GRID_WIDTH - 1)
            y = random.randint(0, GRID_HEIGHT - 1)
            if snake_body is None or (x, y) not in snake_body:
                self.position = (x, y)
                break

def draw_grid(surface):
    for x in range(0, GRID_WIDTH * CELL_SIZE, CELL_SIZE):
        pygame.draw.line(surface, (40, 40, 40), (x, 0), (x, GRID_HEIGHT * CELL_SIZE))
    for y in range(0, GRID_HEIGHT * CELL_SIZE, CELL_SIZE):
        pygame.draw.line(surface, (40, 40, 40), (0, y), (GRID_WIDTH * CELL_SIZE, y))

def main():
    screen = pygame.display.set_mode((GRID_WIDTH * CELL_SIZE, GRID_HEIGHT * CELL_SIZE))
    pygame.display.set_caption("Snake Game")
    clock = pygame.time.Clock()
    font = pygame.font.Font(None, 36)

    snake = Snake()
    food = Food()

    game_over = False
    score = 0

    while True:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit()
                sys.exit()
            if event.type == pygame.KEYDOWN:
                if game_over:
                    if event.key == pygame.K_SPACE:
                        snake.reset()
                        food.randomize(snake.body)
                        game_over = False
                        score = 0
                else:
                    if event.key == pygame.K_UP or event.key == pygame.K_w:
                        snake.set_direction(UP)
                    elif event.key == pygame.K_DOWN or event.key == pygame.K_s:
                        snake.set_direction(DOWN)
                    elif event.key == pygame.K_LEFT or event.key == pygame.K_a:
                        snake.set_direction(LEFT)
                    elif event.key == pygame.K_RIGHT or event.key == pygame.K_d:
                        snake.set_direction(RIGHT)

        if not game_over:
            snake.move()
            if snake.check_collision():
                game_over = True
            elif snake.body[0] == food.position:
                snake.eat()
                score += 1
                food.randomize(snake.body)

        screen.fill(BLACK)
        draw_grid(screen)

        for i, segment in enumerate(snake.body):
            color = GREEN if i == 0 else DARK_GREEN
            rect = pygame.Rect(segment[0] * CELL_SIZE, segment[1] * CELL_SIZE, CELL_SIZE - 1, CELL_SIZE - 1)
            pygame.draw.rect(screen, color, rect)

        food_rect = pygame.Rect(food.position[0] * CELL_SIZE, food.position[1] * CELL_SIZE, CELL_SIZE - 1, CELL_SIZE - 1)
        pygame.draw.rect(screen, RED, food_rect)

        score_text = font.render(f"Score: {score}", True, WHITE)
        screen.blit(score_text, (10, 10))

        if game_over:
            over_text = font.render("GAME OVER - Press SPACE to restart", True, WHITE)
            text_rect = over_text.get_rect(center=(screen.get_width() // 2, screen.get_height() // 2))
            screen.blit(over_text, text_rect)

        pygame.display.flip()
        clock.tick(15)

if __name__ == "__main__":
    main()
