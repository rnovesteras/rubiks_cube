#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Sun Jul 29 06:35:27 2018

@author: intuitionmachine
@author: rnovesteras
"""


"""Class to run unit tests for the TicTacToeGame class."""
from unittest import TestCase

from rubiks_cube.rubiks_cube_game import RubiksCubeGame


class TestRubiksCubeGame(TestCase):
    """Class to run unit tests for the TicTacToeGame class."""
    # TODO: Use methods in train_cube.py for game test checks

    def test_check_game_over_1(self):
        """Test case for the check_game_over function.

        Test for game over with a win.
        """
        game = RubiksCubeGame()
        game.state = [[1, 0, -1], [1, 0, -1], [1, 0, -1]]
        game_over, value = game.check_game_over(1)

        self.assertEqual(game_over, True)
        self.assertEqual(value, 1)

    def test_check_game_over_2(self):
        """Test case for the check_game_over function.

        Test for game over with a loss.
        """
        game = RubiksCubeGame()
        game.state = [[1, 0, -1], [1, 1, 0], [1, -1, -1]]
        game_over, value = game.check_game_over(-1)

        self.assertEqual(game_over, True)
        self.assertEqual(value, -1)

    def test_check_game_over_3(self):
        """Test case for the check_game_over function.

        Test for game over with a draw.
        """
        game = RubiksCubeGame()
        game.state = [[1, 1, -1], [-1, -1, 1], [1, 1, -1]]
        game_over, value = game.check_game_over(1)

        self.assertEqual(game_over, True)
        self.assertEqual(value, 0)

    def test_check_game_over_4(self):
        """Test case for the check_game_over function.

        Test for game not over.
        """
        game = RubiksCubeGame()
        game.state = [[-1, 0, 0], [0, -1, 0], [0, 1, 0]]
        game_over, value = game.check_game_over(-1)

        self.assertEqual(game_over, False)
        self.assertEqual(value, 0)
