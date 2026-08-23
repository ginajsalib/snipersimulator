#!/usr/bin/env python
# Training script for Random Forest model
# Trains a Random Forest classifier on performance data to predict optimal cache and BTB configurations

import sys
import os
import csv
import pickle
import numpy as np
from pathlib import Path
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score, classification_report

# Feature names (must match rf_predict.py)
FEATURE_NAMES = ['ipc', 'l1_miss_rate', 'l2_miss_rate', 'branch_mpki', 'active_cores', 'l1_ways', 'l2_ways', 'btb_entries']

# Target configuration parameter (can be expanded to multi-output if needed)
TARGET_NAME = 'optimal_config_index'

def load_training_data(csv_file):
    """Load training data from CSV file.
    
    Expected CSV format:
    ipc, l1_miss_rate, l2_miss_rate, branch_mpki, active_cores, l1_ways, l2_ways, btb_entries, optimal_config_index
    """
    features = []
    targets = []
    
    try:
        with open(csv_file, 'r') as f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                print(f"Error: CSV file {csv_file} is empty or malformed", file=sys.stderr)
                return None, None
            
            # Verify that all required columns are present
            required_fields = set(FEATURE_NAMES + [TARGET_NAME])
            csv_fields = set(reader.fieldnames)
            
            if not required_fields.issubset(csv_fields):
                print(f"Error: CSV missing required columns. Expected {required_fields}, got {csv_fields}", file=sys.stderr)
                return None, None
            
            for row in reader:
                try:
                    feature_vector = [float(row[name].strip()) for name in FEATURE_NAMES]
                    target = int(row[TARGET_NAME].strip())
                    features.append(feature_vector)
                    targets.append(target)
                except (ValueError, KeyError) as e:
                    print(f"Warning: Skipping malformed row: {row}. Error: {e}", file=sys.stderr)
                    continue
        
        if len(features) == 0:
            print("Error: No valid training samples found in CSV file", file=sys.stderr)
            return None, None
        
        return np.array(features, dtype=np.float32), np.array(targets, dtype=np.int32)
    
    except FileNotFoundError:
        print(f"Error: Training data file {csv_file} not found", file=sys.stderr)
        return None, None
    except Exception as e:
        print(f"Error loading training data: {e}", file=sys.stderr)
        return None, None

def train_rf_model(X_train, y_train, n_estimators=100, max_depth=10, random_state=42):
    """Train Random Forest classifier.
    
    Parameters:
    - n_estimators: number of trees in the forest
    - max_depth: maximum depth of the trees
    - random_state: random seed for reproducibility
    """
    try:
        print(f"Training Random Forest with {n_estimators} trees, max_depth={max_depth}", file=sys.stderr)
        
        model = RandomForestClassifier(
            n_estimators=n_estimators,
            max_depth=max_depth,
            random_state=random_state,
            n_jobs=-1,  # Use all available processors
            verbose=0
        )
        
        model.fit(X_train, y_train)
        print("Training completed successfully", file=sys.stderr)
        return model
    
    except Exception as e:
        print(f"Error training model: {e}", file=sys.stderr)
        return None

def evaluate_model(model, X_test, y_test):
    """Evaluate model on test set."""
    try:
        y_pred = model.predict(X_test)
        accuracy = accuracy_score(y_test, y_pred)
        print(f"Test Accuracy: {accuracy:.4f}", file=sys.stderr)
        print("\nClassification Report:", file=sys.stderr)
        print(classification_report(y_test, y_pred), file=sys.stderr)
        return accuracy
    except Exception as e:
        print(f"Error evaluating model: {e}", file=sys.stderr)
        return None

def save_model(model, model_file):
    """Save trained model to pickle file."""
    try:
        with open(model_file, 'wb') as f:
            pickle.dump(model, f)
        print(f"Model saved to {model_file}", file=sys.stderr)
        return True
    except Exception as e:
        print(f"Error saving model: {e}", file=sys.stderr)
        return False

def print_feature_importances(model):
    """Print feature importances learned by the model."""
    try:
        importances = model.feature_importances_
        print("\nFeature Importances:", file=sys.stderr)
        for name, importance in zip(FEATURE_NAMES, importances):
            print(f"  {name}: {importance:.4f}", file=sys.stderr)
    except Exception as e:
        print(f"Warning: Could not print feature importances: {e}", file=sys.stderr)

def main():
    """Main training function."""
    
    if len(sys.argv) < 2:
        print("Usage: train_rf.py <training_data.csv> [output_model.pkl]", file=sys.stderr)
        print("\nTraining data CSV format:")
        print("  ipc, l1_miss_rate, l2_miss_rate, branch_mpki, active_cores, l1_ways, l2_ways, btb_entries, optimal_config_index", file=sys.stderr)
        return 1
    
    csv_file = sys.argv[1]
    model_file = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(__file__), "rf_model.pkl")
    
    print(f"Loading training data from {csv_file}", file=sys.stderr)
    
    # Load training data
    X, y = load_training_data(csv_file)
    if X is None or y is None:
        print("Failed to load training data", file=sys.stderr)
        return 1
    
    print(f"Loaded {len(X)} training samples with {len(FEATURE_NAMES)} features", file=sys.stderr)
    
    # Split data into training and test sets
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=0.2, random_state=42
    )
    print(f"Training set: {len(X_train)}, Test set: {len(X_test)}", file=sys.stderr)
    
    # Train model
    model = train_rf_model(X_train, y_train, n_estimators=100, max_depth=10)
    if model is None:
        print("Failed to train model", file=sys.stderr)
        return 1
    
    # Evaluate on training set
    train_accuracy = model.score(X_train, y_train)
    print(f"Training Accuracy: {train_accuracy:.4f}", file=sys.stderr)
    
    # Evaluate on test set
    evaluate_model(model, X_test, y_test)
    
    # Print feature importances
    print_feature_importances(model)
    
    # Save model
    if not save_model(model, model_file):
        return 1
    
    print(f"\nTraining complete. Model saved to: {model_file}", file=sys.stderr)
    return 0

if __name__ == "__main__":
    sys.exit(main())
