'use client';

import { Component, type ReactNode } from 'react';

interface Props {
  children: ReactNode;
}

interface State {
  hasError: boolean;
  message: string;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, message: '' };

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, message: error.message };
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="flex flex-col items-center justify-center min-h-[50vh] gap-4 text-center p-8">
          <h2 className="text-xl font-semibold text-text">Something went wrong</h2>
          {this.state.message && (
            <p className="text-sm text-muted max-w-md">{this.state.message}</p>
          )}
          <button
            className="px-4 py-2 bg-info text-white rounded-md hover:bg-info transition-colors"
            onClick={() => this.setState({ hasError: false, message: '' })}
          >
            Try again
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
