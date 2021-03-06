"""
End to end training of my neural network model.

The training routine has three key phases

- Evaluation through MCTS
- Data generation through MCTS
- Neural network training
"""
import numpy as np
from collections import defaultdict, deque, Counter, namedtuple
import itertools
import warnings
import os, pathlib, psutil # useful for memory management
from datetime import datetime
import git # for keeping track of git versions

from mcts_nn_cube import State, MCTSAgent
import models
#from pympler import tracker
#tr1 = tracker.SummaryTracker()
#tr2 = tracker.SummaryTracker()

def get_current_version():
    return git.Git().describe('--match', 'v?.*', '--match', 'v??.*', '--match', 'v???.*', '--dirty')

def create_directory(dir_path):
    """
    dir_path is a pathlib.Path object

    Creates a directory, including parents.  If the directory exists, do nothing.
    """
    try:
        dir_path.mkdir(parents=True) # make ../results/<current_version> directory (and parent dir) if it doesn't exist
    except FileExistsError:
        pass


# memory management
MY_PROCESS = psutil.Process(os.getpid())
def memory_used():
    return MY_PROCESS.memory_info().rss

def str_between(s, start, end):
    return (s.split(start))[1].split(end)[0]


class GameAgent():
    def __init__(self, game_id):
        self.game_id = game_id
        self.self_play_stats=defaultdict(list)
        self.game_stats=defaultdict(list)
        self.data_states = []
        self.data_policies = []
        self.data_values = []
        self.counter=0
        self.done=False
        self.win=False
        # can attach other attributes as needed

class BatchGameAgent():
    """
    Handles the steps of the games, including batch games.
    """
    def __init__(self, model, max_steps, max_depth, min_game_length, max_game_length, transposition_table, decay, exploration, dirichlet_const):
        self.game_agents = deque()
        self.model = model
        self.max_depth = max_depth
        self.max_steps = max_steps
        self.min_game_length = min_game_length
        self.max_game_length = max_game_length
        self.transposition_table = transposition_table
        self.exploration = exploration
        self.decay = decay
        self.dirichlet_const = dirichlet_const

    def is_empty(self):
        return not bool(self.game_agents)

    def append_states(self, state_info_iter):
        for game_id, state, distance, distance_level in state_info_iter:
            mcts = MCTSAgent(self.model.function, 
                             state, 
                             max_depth = self.max_depth, 
                             transposition_table = self.transposition_table.copy() if self.transposition_table is not None else None,
                             c_puct = self.exploration,
                             gamma = self.decay,
                             dirichlet_const = self.dirichlet_const)
            
            game_agent = GameAgent(game_id)
            game_agent.mcts = mcts
            game_agent.distance = distance
            game_agent.distance_level = distance_level

            self.game_agents.append(game_agent)

    def run_game_agent_one_step(self, game_agent):
        mcts = game_agent.mcts
        mcts.search(steps=self.max_steps)

        # reduce the max batch size to prevent the worker from blocking
        self.model.set_max_batch_size(self.model.get_max_batch_size() - 1)

    def process_completed_step(self, game_agent):
        mcts = game_agent.mcts
            
        # find next state
        probs = mcts.action_probabilities(inv_temp = 10)
        action = np.argmax(probs)
        #action = np.random.choice(12, p=probs)

        shortest_path = game_agent.mcts.stats('shortest_path')

        # record stats
        game_agent.self_play_stats['_game_id'].append(game_agent.game_id)
        game_agent.self_play_stats['_step_id'].append(game_agent.counter)
        game_agent.self_play_stats['shortest_path'].append(shortest_path)
        game_agent.self_play_stats['action'].append(action)
        game_agent.self_play_stats['value'].append(mcts.stats('value'))

        game_agent.self_play_stats['prior'].append(mcts.stats('prior'))
        game_agent.self_play_stats['prior_dirichlet'].append(mcts.stats('prior_dirichlet'))
        game_agent.self_play_stats['visit_counts'].append(mcts.stats('visit_counts'))
        game_agent.self_play_stats['total_action_values'].append(mcts.stats('total_action_values'))

        # training data (also recorded in stats)
        game_agent.data_states.append(mcts.initial_node.state.input_array_no_history())
        
        policy = mcts.action_probabilities(inv_temp = 10)
        game_agent.data_policies.append(policy)
        game_agent.self_play_stats['updated_policy'].append(policy)
        
        game_agent.data_values.append(0) # updated if game is success
        game_agent.self_play_stats['updated_value'].append(0)

        # prepare for next state
        game_agent.counter += 1 
        #if shortest_path < 0:
        #    print("(DB) no path")
        if (game_agent.counter >= self.min_game_length and shortest_path < 0) or game_agent.counter >= self.max_game_length:
            game_agent.win = False
            game_agent.done = True
        else:
            mcts.advance_to_action(action)
            if mcts.is_terminal():
                game_agent.win = True
                game_agent.done = True

    def run_one_step_with_threading(self):
        import threading
        # start threads
        self.model.set_max_batch_size(len(self.game_agents))

        threads = []
        for game_agent in self.game_agents:
            t = threading.Thread(target=self.run_game_agent_one_step, args=(game_agent, ))
            t.start()
            threads.append(t)

        # wait for threads to finish
        for t in threads:
            t.join()

        for game_agent in self.game_agents:
            self.process_completed_step(game_agent)

    def run_one_step(self):
        for game_agent in self.game_agents:

            mcts = game_agent.mcts
            mcts.search(steps=self.max_steps)
            
            self.process_completed_step(game_agent)

    def finished_game_results(self):
        for _ in range(len(self.game_agents)):
            game_agent = self.game_agents.popleft()

            if not game_agent.done:
                self.game_agents.append(game_agent)
            else:
                if game_agent.win:
                    value = 1
                    for i in range(game_agent.counter):
                        value *= self.decay
                        game_agent.data_values[-(i+1)] = value
                        game_agent.self_play_stats['updated_value'][-(i+1)] = value
          
                # record game stats
                game_agent.game_stats['_game_id'].append(game_agent.game_id)
                game_agent.game_stats['distance_level'].append(game_agent.distance_level)
                game_agent.game_stats['training_distance'].append(game_agent.distance)
                game_agent.game_stats['min_game_length'].append(self.min_game_length)
                game_agent.game_stats['max_game_length'].append(self.max_game_length)
                game_agent.game_stats['win'].append(game_agent.win)
                game_agent.game_stats['total_steps'].append(game_agent.counter if game_agent.win else -1)

                yield game_agent

class TrainingAgent():
    """
    This agent handles all the details of the training.
    """
    def __init__(self, config):
        """
        Takes as input a config module with common settings.
        """

        # Versions and Directories
        self.current_version = get_current_version()
        self.versions = [self.current_version] + config.prev_versions
        
        self.results_dir = pathlib.Path(config.results_dir)
        self.current_version_dir = self.results_dir.joinpath(self.current_version)
        create_directory(self.current_version_dir) # create directory if it doesn't exist
        
        self.save_dir = pathlib.Path(config.save_dir) # for backwards compatibility

        # Threading
        self.multithreaded = config.multithreaded

        # Model (NN) parameters (fixed)
        self.prev_state_history = config.prev_state_history # the number of previous states (including the current one) used as input to the model
        
        ModelType = models.__dict__[config.model_type] # get model class by name
        self.checkpoint_model = ModelType(**config.model_kwargs) # this doesn't build and/or load the model yet
        self.best_model = ModelType(**config.model_kwargs) # this doesn't build and/or load the model yet
        
        if self.multithreaded:
            self.checkpoint_model.multithreaded = True
            self.best_model.multithreaded = True
        
        # Model training parameters (fixed)
        self.learning_rate = config.learning_rate
        self.augment_training_data = config.augment_training_data

        # MCTS parameters (fixed)
        self.max_depth = config.max_depth
        self.max_steps = config.max_steps
        self.use_prebuilt_transposition_table = config.use_prebuilt_transposition_table
        self.use_transposition_table = config.use_transposition_table
        self.decay = config.decay # gamma
        self.exploration = config.exploration # c_puct
        self.dirichlet_const = config.dirichlet_const # alpha (None if no Dirichlet noise)

        self.prebuilt_transposition_table = None # built later


        # Validation flags
        self.validate_training_data = config.validate_training_data

        # Training parameters (fixed)
        self.batch_size = config.batch_size
        self.games_per_generation = config.games_per_generation
        self.starting_distance = config.starting_distance
        self.min_distance = config.min_distance
        self.win_rate_target = config.win_rate_target
        self.min_game_length = config.min_game_length
        self.max_game_length = config.max_game_length
        self.prev_generations_used_for_training = config.prev_generations_used_for_training
        self.training_sample_ratio = config.training_sample_ratio
        self.games_per_evaluation = config.games_per_evaluation
        self.win_margin_to_become_best_model = config.win_margin_to_become_best_model

        # Training parameters preserved between generations
        self.training_distance_level = float(self.starting_distance)
        self.recent_wins = Counter()
        self.recent_games = Counter()
        self.checkpoint_training_distance_level = float(self.starting_distance)
        self.checkpoint_recent_wins = Counter()
        self.checkpoint_recent_games = Counter()

        # Training parameters (dynamic)
        self.game_number = 0
        self.self_play_start = None # date and time (utc)
        self.self_play_end = None
        self.training_start = None
        self.training_end = None

        # Evaluation parameters (dynamic)
        self.generation = 0
        self.best_generation = 0

        # Self play stats
        # These are functionally data tables implemented as a dictionary of lists
        # The keys are the column names.  This makes it easy to change the stats I am recording.
        self.self_play_stats = defaultdict(list)
        self.game_stats = defaultdict(list)
        self.training_stats = defaultdict(list)
        self.generation_stats = defaultdict(list)

        # Training data
        self.training_data_states = []
        self.training_data_policies = []
        self.training_data_values = []

    def build_models(self):
        """
        Builds both checkpoint and best model
        May be overwritten later by loaded weights
        """
        self.checkpoint_model.build()
        self.best_model.build()

    def filepaths_by_generation(self, filetype, version):
        # pattern
        glob_pattern = '{}_{}_gen*.h5'.format(filetype, version)

        # first check new saving scheme: ../results/<version>/filename
        version_dir = self.results_dir.joinpath(version)

        if not version_dir.exists():
            # try ../save/filename for backwards compatibility
            version_dir = self.save_dir

        if not version_dir.exists():
            return [] # no files found
        
        file_paths = version_dir.glob(glob_pattern)

        # sort by generation
        gen_paths = sorted([(int(str_between(str(f), "_gen", ".h5")), f) for f in file_paths])
        
        return gen_paths

    def new_filepath(self, filetype):
        file_name = "{}_{}_gen{:03}.h5".format(filetype, self.versions[0], self.generation)
        return self.current_version_dir.joinpath(file_name)

    def load_transposition_table(self):
        #TODO: Add this.  For now, just use empty table.

        warnings.warn("load_transposition_table is not properly implemented", stacklevel=2)

        if self.use_transposition_table:
            self.prebuilt_transposition_table = {}
        else:
            self.prebuilt_transposition_table = None

    def load_models(self):
        """ 
        Finds the checkpoint model and the best model in the given naming scheme 
        and loads those
        """
        import os

        # load checkpoint model
        
        for version in self.versions:
            model_files = self.filepaths_by_generation('checkpoint_model', version)

            if model_files:
                # choose newest generation
                gen, path = model_files[-1]
                
                print("checkpoint model found: '{}'".format(path))
                print("loading model ...")
                self.checkpoint_model.load_from_file(path)

                self.generation = gen
                break

            else:
                print("no checkpoint model found with version {}".format(version))
        
        print("generation set to", self.generation)

        # load best model
        for version in self.versions:
            model_files = self.filepaths_by_generation('model', version)

            if model_files:
                # choose newest generation
                gen, path = model_files[-1]
                
                print("best model found: '{}'".format(path))
                print("loading model ...")
                self.best_model.load_from_file(path)

                self.best_generation = gen
                break

            else:
                print("no best model found with version {}".format(version)) 

        print("best generation:", self.best_generation)

    def save_checkpoint_model(self):
        path = self.new_filepath('checkpoint_model')
        self.checkpoint_model.save_to_file(path)
        print("saved model checkpoint: '{}'".format(path))

        self.checkpoint_training_distance_level = self.training_distance_level
        self.checkpoint_recent_wins = Counter()
        self.checkpoint_recent_games = Counter()
        # add a few free wins to speed up the convergence
        for dist in range(int(self.training_distance_level) + 1):
            self.checkpoint_recent_games[dist] += 1
            self.checkpoint_recent_wins[dist] += 1

    def save_and_set_best_model(self):
        path = self.new_filepath('model')
        self.checkpoint_model.save_to_file(path)
        print("saved model: '{}'".format(path))

        self.best_model.load_from_file(path)

        self.best_generation = self.generation
        self.training_distance_level = self.checkpoint_training_distance_level
        self.recent_wins = self.checkpoint_recent_wins
        self.recent_games = self.checkpoint_recent_games

    def train_model(self):
        import os
        import h5py

        inputs_list = []
        outputs_policy_list = []
        outputs_value_list = []

        counter = 0
        for version in self.versions:
            if counter > self.prev_generations_used_for_training:
                break

            data_files = self.filepaths_by_generation('data', version)
            
            # go through in reverse order
            for gen, path in reversed(data_files):
                if counter > self.prev_generations_used_for_training:
                    break

                print("loading data: '{}'".format(path))

                with h5py.File(path, 'r') as hf:
                    inputs_list.append(hf['inputs'][:])
                    outputs_policy_list.append(hf['outputs_policy'][:])
                    outputs_value_list.append(hf['outputs_value'][:])

                counter += 1

        inputs_all = np.concatenate(inputs_list, axis=0)
        outputs_policy_all = np.concatenate(outputs_policy_list, axis=0)
        outputs_value_all = np.concatenate(outputs_value_list, axis=0)

        if self.validate_training_data:
            print("validating data...")
            self.checkpoint_model.validate_data(inputs_all, outputs_policy_all, outputs_value_all, gamma=self.decay)
            self.validate_training_data = False # just validate for first round
            print("data valid.")

        print("processing...")
        inputs_all, outputs_policy_all, outputs_value_all = \
            self.checkpoint_model.process_training_data(inputs_all, outputs_policy_all, outputs_value_all, augment=self.augment_training_data)

        n = len(inputs_all)
        sample_size = int((n * self.training_sample_ratio) // 32 + 1) * 32 # roughly self.training_sample_ratio % of samples
        sample_idx = np.random.choice(n, size=sample_size)
        inputs = inputs_all[sample_idx]
        outputs_policy = outputs_policy_all[sample_idx]
        outputs_value = outputs_value_all[sample_idx]

        print("training...")
        self.checkpoint_model.train_on_data([inputs, outputs_policy, outputs_value])

    def reset_self_play(self):
        # Training parameters (dynamic)
        self.game_number = 0
        self.self_play_start = None # date and time (utc)
        self.self_play_end = None
        self.training_start = None
        self.training_end = None

        # Self play stats
        self.self_play_stats = defaultdict(list)
        self.game_stats = defaultdict(list)
        self.generation_stats = defaultdict(list)

        # Training data (one item per game based on randomly chosen game state)
        self.training_data_states = []
        self.training_data_policies = []
        self.training_data_values = []

        # set start time
        self.self_play_start = datetime.utcnow() # date and time (utc)

    def save_training_stats(self):
        import pandas as pd

        path = self.new_filepath('stats')

        # record time of end of self-play
        self.self_play_end = datetime.utcnow()

        # save generation_stats data
        self.generation_stats['_generation'].append(self.generation)
        self.generation_stats['best_model_generation'].append(self.best_generation)
        self.generation_stats['distance_level'].append(self.training_distance_level)
        self.generation_stats['memory_usage'].append(memory_used())
        self.generation_stats['version_history'].append(",".join(self.versions))
        self.generation_stats['self_play_start_datetime_utc'].append(str(self.self_play_start))
        self.generation_stats['self_play_end_datetime_utc'].append(str(self.self_play_end))
        self.generation_stats['self_play_time_sec'].append((self.self_play_end - self.self_play_start).total_seconds())
        
        generation_stats_df = pd.DataFrame(data=self.generation_stats)
        generation_stats_df.to_hdf(path, 'generation_stats', mode='a', format='fixed') #use mode='a' to avoid overwriting

        # save game_stats data
        game_stats_df = pd.DataFrame(data=self.game_stats)
        game_stats_df.to_hdf(path, 'game_stats', mode='a', format='fixed')
        
        # save self_play_stats data
        self_play_stats_df = pd.DataFrame(data=self.self_play_stats)
        self_play_stats_df.to_hdf(path, 'self_play_stats', mode='a', format='fixed') #use mode='a' to avoid overwriting

        print("saved stats: '{}'".format(path))

    def save_training_data(self):
        # save training_data
        import h5py

        path = self.new_filepath('data')

        inputs, outputs_policy, outputs_value = \
            self.best_model.preprocess_training_data(self.training_data_states,
                                                  self.training_data_policies,
                                                  self.training_data_values)

        if self.validate_training_data:
            print("validating data...")
            self.best_model.validate_data(inputs, outputs_policy, outputs_value, gamma=self.decay)
            print("data valid.")

        with h5py.File(path, 'w') as hf:
            hf.create_dataset("inputs",  data=inputs)
            hf.create_dataset("outputs_policy",  data=outputs_policy)
            hf.create_dataset("outputs_value",  data=outputs_value)

        print("saved data: '{}'".format(path))

    @staticmethod
    def random_state(distance, history):
        state = State(random_depth = distance, history = history)
        while state.done(): 
            state = State(random_depth = distance, history = history)
        return state

    @staticmethod
    def random_distance(distance_level):
        lower_dist = int(distance_level)
        prob_of_increase = distance_level - lower_dist
        distance = lower_dist + np.random.choice(2, p=[1-prob_of_increase, prob_of_increase])
        return distance

    def update_win_and_level(self, distance, win, checkpoint=False):
        if checkpoint:
            training_distance_level = self.checkpoint_training_distance_level
            recent_wins = self.checkpoint_recent_wins
            recent_games = self.checkpoint_recent_games
        else:
            training_distance_level = self.training_distance_level
            recent_wins = self.recent_wins
            recent_games = self.recent_games

        # update wins/loses
        recent_wins[distance] += win
        recent_games[distance] += 1

        # update difficulty
        upper_dist = 0
        while True:
            upper_dist += 1
            if recent_wins[upper_dist] <= self.win_rate_target * recent_games[upper_dist]:
                break
        if upper_dist <= self.min_distance:
            training_distance_level = float(self.min_distance)
        else:
            lower_dist = upper_dist - 1
            lower_dist_win_rate = (.99 * self.win_rate_target) if recent_games[lower_dist] == 0 \
                                    else recent_wins[lower_dist] / recent_games[lower_dist]
            upper_dist_win_rate = (.99 * self.win_rate_target) if recent_games[lower_dist+1] == 0 \
                                    else recent_wins[lower_dist+1] / recent_games[lower_dist+1]
            # notice that we won't divide by zero here since upper_dist_win_rate < lower_dist_win_rate
            training_distance_level = lower_dist + (lower_dist_win_rate - self.win_rate_target) / (lower_dist_win_rate - upper_dist_win_rate)

        if checkpoint:
            self.checkpoint_training_distance_level = training_distance_level
        else:
            self.training_distance_level = training_distance_level

    def print_game_stats(self, game_results):
        game_id = game_results.game_id
        distance = game_results.distance
        level = game_results.distance_level
        win = game_results.win
        steps = game_results.game_stats['total_steps'][0]
        lost_way = game_results.self_play_stats['shortest_path'][0] < 0

        print("\nGame {}/{}".format(game_id, self.games_per_generation))
        print("distance: {} (level: {:.2f})".format(distance, level))
        if win:
            print("win ({}{} steps)".format(steps, "*" if lost_way else ""))
        else:
            print("loss")
        print()
        new_level = self.training_distance_level
        lower_dist = int(new_level)
        lower_dist_win_rate = float('nan') if self.recent_games[lower_dist] == 0 else self.recent_wins[lower_dist] / self.recent_games[lower_dist]
        upper_dist_win_rate = float('nan') if self.recent_games[lower_dist+1] == 0 else self.recent_wins[lower_dist+1] / self.recent_games[lower_dist+1]
        print("(DB) new level: {:.2f}, win rates: {}: {:.2f} {}: {:.2f}".format(new_level, lower_dist, lower_dist_win_rate, lower_dist+1, upper_dist_win_rate))
        print(end="", flush=True) # force stdout to flush (fixes buffering issues)

    def print_eval_game_stats(self, game_results1, game_results2, current_scores):
        game_id1 = game_results1.game_id
        game_id2 = game_results2.game_id
        distance1 = game_results1.distance
        distance2 = game_results2.distance
        level1 = game_results1.distance_level
        level2 = game_results2.distance_level
        win1 = game_results1.win
        win2 = game_results2.win
        steps1 = game_results1.game_stats['total_steps'][0]
        steps2 = game_results2.game_stats['total_steps'][0]
        lost_way1 = game_results1.self_play_stats['shortest_path'][0] < 0
        lost_way2 = game_results2.self_play_stats['shortest_path'][0] < 0
        assert game_id1 == game_id2
        assert distance1 == distance2
        print("\nEvaluation Game {}/{}".format(game_id1, self.games_per_evaluation))
        print("distance: {} (levels: {:.2f} {:.2f})".format(distance1, level1, level2))
        if win1:
            print("best model:       win ({}{} steps)".format(steps1, "*" if lost_way1 else ""))
        else:
            print("best model:       loss")
        if win2:
            print("checkpoint model: win ({}{} steps)".format(steps2, "*" if lost_way2 else ""))
        else:
            print("checkpoint model: loss")

        print()
        new_level = self.training_distance_level
        recent_games = self.recent_games
        recent_wins = self.recent_wins
        lower_dist = int(new_level)
        lower_dist_win_rate = float('nan') if recent_games[lower_dist] == 0 else recent_wins[lower_dist] / recent_games[lower_dist]
        upper_dist_win_rate = float('nan') if recent_games[lower_dist+1] == 0 else recent_wins[lower_dist+1] / recent_games[lower_dist+1]
        print("(DB) best model new level: {:.2f}, win rates: {}: {:.2f} {}: {:.2f}".format(new_level, lower_dist, lower_dist_win_rate, lower_dist+1, upper_dist_win_rate))
        
        new_level = self.checkpoint_training_distance_level
        recent_games = self.checkpoint_recent_games
        recent_wins = self.checkpoint_recent_wins
        lower_dist = int(new_level)
        lower_dist_win_rate = float('nan') if recent_games[lower_dist] == 0 else recent_wins[lower_dist] / recent_games[lower_dist]
        upper_dist_win_rate = float('nan') if recent_games[lower_dist+1] == 0 else recent_wins[lower_dist+1] / recent_games[lower_dist+1]
        print("(DB) checkpoint new level: {:.2f}, win rates: {}: {:.2f} {}: {:.2f}".format(new_level, lower_dist, lower_dist_win_rate, lower_dist+1, upper_dist_win_rate))
        print(end="", flush=True) # force stdout to flush (fixes buffering issues)

    def state_generator(self, max_game_id, evaluation=False):
        while self.game_number < max_game_id:
            if evaluation:
                distance_level = max(self.training_distance_level, self.checkpoint_training_distance_level)
            else:
                distance_level = self.training_distance_level 
            distance = self.random_distance(distance_level)
            state = self.random_state(distance, self.prev_state_history)

            print("(DB)", "starting game", self.game_number, "...")
            yield self.game_number, state, distance, distance_level

            self.game_number += 1

    def game_generator(self, model, state_generator, max_batch_size, return_in_order):
        """
        Send games to the batch game agent and retrieve the finished games.
        Yield the finished games in consecutive order of their id.
        """
        import heapq
        finished_games = [] # priority queue

        batch_game_agent = BatchGameAgent(model=model,
                                          max_steps=self.max_steps, 
                                          max_depth=self.max_depth,
                                          min_game_length=self.min_game_length, 
                                          max_game_length=self.max_game_length, 
                                          transposition_table=self.prebuilt_transposition_table,
                                          decay=self.decay, 
                                          exploration=self.exploration,
                                          dirichlet_const=self.dirichlet_const) 

        # scale batch size up to make for better beginning determination of distance level
        # use batch size of 1 for first 16 games
        batch_size = 1
        cnt = 16

        # attach inital batch
        first_batch = list(itertools.islice(state_generator, batch_size))
        if not first_batch:
            return
        batch_game_agent.append_states(first_batch)
        next_game_id = first_batch[0][0] # game_id is first element

        # loop until all done
        while not batch_game_agent.is_empty():
            if self.multithreaded:
                batch_game_agent.run_one_step_with_threading()
            else:
                batch_game_agent.run_one_step()

            # collect all finished games
            for game_results in batch_game_agent.finished_game_results():
                heapq.heappush(finished_games, (game_results.game_id, game_results))

            # check if available slots
            if len(batch_game_agent.game_agents) < batch_size:
                
                # increment batch size
                cnt -= 1
                if cnt < 0:
                    batch_size = max_batch_size

            if return_in_order:
                # return those which are next in order
                if not finished_games or finished_games[0][1].game_id != next_game_id:
                    print("(DB)", "waiting on game", next_game_id, "(finished games:", ",".join(str(g[1].game_id) for g in finished_games), ") ...")

                while finished_games and finished_games[0][1].game_id == next_game_id:
                    yield heapq.heappop(finished_games)[1]
                    next_game_id += 1
            else:
                # return in order they are finished
                if not finished_games:
                    print("(DB) ...")

                while finished_games:
                    yield heapq.heappop(finished_games)[1]

            # fill up the batch (do after yields to ensure that self.training_distance_level is updated)
            available_slots = batch_size - len(batch_game_agent.game_agents)
            replacement_batch = itertools.islice(state_generator, available_slots)
            batch_game_agent.append_states(replacement_batch)

    def generate_data_self_play(self):
        # don't reset self_play since using the evaluation results to also get data
        #self.reset_self_play()

        for game_results in self.game_generator(self.best_model, self.state_generator(self.games_per_generation), max_batch_size=self.batch_size, return_in_order=False):
            # update data
            for k, v in game_results.self_play_stats.items():
                self.self_play_stats[k] += v
            for k, v in game_results.game_stats.items():
                self.game_stats[k] += v
            self.training_data_states += game_results.data_states
            self.training_data_policies += game_results.data_policies
            self.training_data_values += game_results.data_values
            
            # update win rates and level
            self.update_win_and_level(game_results.distance, game_results.win)

            # Print details
            self.print_game_stats(game_results)

    def evaluate_and_choose_best_model(self):
        self.reset_self_play()

        state_generator1, state_generator2 = itertools.tee(self.state_generator(self.games_per_evaluation, evaluation=True))

        best_model_wins = 0
        checkpoint_model_wins = 0
        ties = 0

        for game_results1, game_results2 \
            in zip(self.game_generator(self.best_model, state_generator1, max_batch_size=self.batch_size, return_in_order=True), 
                   self.game_generator(self.checkpoint_model, state_generator2, max_batch_size=self.batch_size, return_in_order=True)):

            if game_results1.win > game_results2.win:
                best_model_wins += 1
                game_results = game_results1
            elif game_results1.win < game_results2.win:
                checkpoint_model_wins += 1
                game_results = game_results2
            else:
                ties += 1
                game_results = game_results1

            # update data
            for k, v in game_results.self_play_stats.items():
                self.self_play_stats[k] += v
            for k, v in game_results.game_stats.items():
                self.game_stats[k] += v
            self.training_data_states += game_results.data_states
            self.training_data_policies += game_results.data_policies
            self.training_data_values += game_results.data_values

            # update win rates and level
            self.update_win_and_level(game_results1.distance, game_results1.win)
            self.update_win_and_level(game_results2.distance, game_results2.win, checkpoint=True)

            # Print details
            self.print_eval_game_stats(game_results1, game_results2, [best_model_wins, checkpoint_model_wins, ties])

        print("\nEvaluation results (win/lose/tie)")
        print("Best model      : {:2} / {:2} / {:2}".format(best_model_wins, checkpoint_model_wins, ties))
        print("Checkpoint model: {:2} / {:2} / {:2}".format(checkpoint_model_wins, best_model_wins, ties))
        
        if checkpoint_model_wins - best_model_wins >= self.win_margin_to_become_best_model:
            print("\nCheckpoint model is better.")
            print("\nSave and set as best model...")
            self.save_and_set_best_model()
        else:
            print("\nCurrent best model is still the best.")

def main(config):
    agent = TrainingAgent(config)

    print("Build models...")
    agent.build_models()

    print("\nLoad pre-built transposition table...")
    agent.load_transposition_table()

    print("\nLoad models (if any)...")
    agent.load_models()
    
    print("\nBegin training loop...")
    agent.reset_self_play()

    #print("BBB", "tr1:")
    #tr1.print_diff()
    #print("BBB", "tr2:")
    #tr2.print_diff()

    while True:
        print("\nBegin self-play data generation...")
        agent.generate_data_self_play()

        #print("BBB", "tr2:")
        #tr2.print_diff()

        print("\nSave stats...")
        agent.save_training_stats()

        #print("BBB", "tr1:")
        #tr1.print_diff()
        #print("BBB", "tr2:")
        #tr2.print_diff()

        print("\nSave data...")
        agent.save_training_data()

        #print("BBB", "tr2:")
        #tr2.print_diff()

        agent.generation += 1

        print("\nTrain model...")
        agent.train_model()

        #print("BBB", "tr2:")
        #tr2.print_diff()

        print("\nSave model...")
        agent.save_checkpoint_model()   

        #print("BBB", "tr2:")
        #tr2.print_diff()

        print("\nBegin evaluation...")
        agent.evaluate_and_choose_best_model()

        #print("BBB", "tr2:")
        #tr2.print_diff()
        
if __name__ == '__main__':
    import config  # configuration file containing frequently changed parameters

    try:
        main(config)
    except KeyboardInterrupt:
        print("\nExiting the program...\nGood bye!")
    finally:
        pass
    
    