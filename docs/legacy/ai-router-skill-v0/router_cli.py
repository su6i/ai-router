# FROZEN REFERENCE — not executed.
# Origin: agent-constitution skills/ai-router @ 9b2be20 (2026-07-01).
# See docs/legacy/README.md.
#!/usr/bin/env python3
"""
AI Router CLI
Command-line interface for the AI Router
"""

import asyncio
import argparse
import json
import sys
from pathlib import Path
from typing import Optional
import os

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from ai_router import AIRouter, ModelType, TaskComplexity
from config_example import MODEL_CONFIGS, BALANCED_ROUTING, CONSERVATIVE_ROUTING, COST_OPTIMIZED_ROUTING


class Colors:
    """ANSI color codes for terminal output"""
    HEADER = '\033[95m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'


def print_colored(text: str, color: str):
    """Print colored text to terminal"""
    print(f"{color}{text}{Colors.ENDC}")


def print_header(text: str):
    """Print formatted header"""
    print_colored(f"\n{'='*60}", Colors.BLUE)
    print_colored(text.center(60), Colors.BOLD)
    print_colored(f"{'='*60}\n", Colors.BLUE)


def print_response(response: dict):
    """Pretty print response"""
    print_colored("\n📝 Response:", Colors.BOLD)
    print(f"\n{response['response']}\n")
    
    print_colored("📊 Metadata:", Colors.BOLD)
    print(f"Model: {Colors.CYAN}{response['model']}{Colors.ENDC}")
    print(f"Cost: {Colors.GREEN}${response['cost_usd']:.4f}{Colors.ENDC}")
    print(f"Tokens: {response['input_tokens']} in + {response['output_tokens']} out")
    print(f"Latency: {response['latency_ms']:.0f}ms")
    
    if response.get('cache_hit'):
        print_colored("✅ Cache Hit!", Colors.GREEN)


def print_stats(stats: dict):
    """Pretty print statistics"""
    print_header("📈 Router Statistics")
    
    if 'cost_summary' in stats:
        summary = stats['cost_summary']
        print_colored("Cost Summary:", Colors.BOLD)
        print(f"Total Requests: {summary['total_requests']}")
        print(f"Total Cost: {Colors.GREEN}${summary['total_cost']}{Colors.ENDC}")
        print(f"Avg Cost/Request: ${summary['avg_cost_per_request']:.4f}")
        print(f"Cache Hit Rate: {summary['cache_hit_rate']*100:.1f}%")
        print(f"Error Rate: {summary['error_rate']*100:.1f}%")
        
        if 'model_breakdown' in summary:
            print_colored("\nPer-Model Breakdown:", Colors.BOLD)
            for model, data in summary['model_breakdown'].items():
                print(f"\n  {Colors.CYAN}{model}{Colors.ENDC}:")
                print(f"    Requests: {data['requests']}")
                print(f"    Cost: ${data['cost']:.4f}")
                print(f"    Avg Latency: {data['avg_latency_ms']:.0f}ms")
    
    if 'cache_stats' in stats:
        cache = stats['cache_stats']
        print_colored("\nCache Statistics:", Colors.BOLD)
        print(f"Entries: {cache['total_entries']}")
        print(f"Memory: {cache['memory_usage_mb']:.2f} MB")


async def interactive_mode(router: AIRouter):
    """Run in interactive mode"""
    print_header("🤖 AI Router - Interactive Mode")
    print_colored("Type 'exit' to quit, 'stats' for statistics, 'help' for commands\n", Colors.YELLOW)
    
    while True:
        try:
            prompt = input(f"{Colors.GREEN}You: {Colors.ENDC}").strip()
            
            if not prompt:
                continue
            
            if prompt.lower() == 'exit':
                print_colored("\n👋 Goodbye!", Colors.BLUE)
                break
            
            if prompt.lower() == 'stats':
                stats = await router.get_stats()
                print_stats(stats)
                continue
            
            if prompt.lower() == 'help':
                print_colored("\nAvailable Commands:", Colors.BOLD)
                print("  exit   - Exit the program")
                print("  stats  - Show router statistics")
                print("  help   - Show this help message")
                print("  clear  - Clear screen")
                print("\nSpecial Flags:")
                print("  @opus   - Force Claude Opus")
                print("  @sonnet - Force Claude Sonnet")
                print("  @haiku  - Force Claude Haiku")
                print("  @deepseek - Force DeepSeek")
                print()
                continue
            
            if prompt.lower() == 'clear':
                os.system('clear' if os.name != 'nt' else 'cls')
                continue
            
            # Check for model override
            force_model = None
            if prompt.startswith('@'):
                parts = prompt.split(' ', 1)
                if len(parts) == 2:
                    model_name, prompt = parts
                    model_map = {
                        '@opus': ModelType.CLAUDE_OPUS,
                        '@sonnet': ModelType.CLAUDE_SONNET,
                        '@haiku': ModelType.CLAUDE_HAIKU,
                        '@deepseek': ModelType.DEEPSEEK_CODER,
                    }
                    force_model = model_map.get(model_name.lower())
                    if force_model:
                        print_colored(f"Forcing model: {force_model.value}", Colors.YELLOW)
            
            # Generate response
            print_colored("\n🤔 Thinking...", Colors.YELLOW)
            response = await router.generate(
                prompt,
                force_model=force_model
            )
            
            print_response(response)
            print()
            
        except KeyboardInterrupt:
            print_colored("\n\n👋 Goodbye!", Colors.BLUE)
            break
        except Exception as e:
            print_colored(f"\n❌ Error: {str(e)}", Colors.RED)


async def single_prompt_mode(router: AIRouter, prompt: str, force_model: Optional[str] = None):
    """Process a single prompt"""
    model_map = {
        'opus': ModelType.CLAUDE_OPUS,
        'sonnet': ModelType.CLAUDE_SONNET,
        'haiku': ModelType.CLAUDE_HAIKU,
        'deepseek': ModelType.DEEPSEEK_CODER,
    }
    
    force = model_map.get(force_model) if force_model else None
    
    response = await router.generate(prompt, force_model=force)
    print_response(response)


async def batch_mode(router: AIRouter, input_file: str, output_file: Optional[str] = None):
    """Process multiple prompts from a file"""
    print_header("📦 Batch Processing Mode")
    
    # Read prompts
    with open(input_file, 'r') as f:
        prompts = [line.strip() for line in f if line.strip()]
    
    print(f"Processing {len(prompts)} prompts...")
    
    # Process all prompts concurrently
    tasks = [router.generate(prompt) for prompt in prompts]
    responses = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Prepare results
    results = []
    for i, (prompt, response) in enumerate(zip(prompts, responses), 1):
        if isinstance(response, Exception):
            print_colored(f"❌ Prompt {i} failed: {str(response)}", Colors.RED)
            results.append({
                'prompt': prompt,
                'error': str(response)
            })
        else:
            print_colored(f"✅ Prompt {i} completed", Colors.GREEN)
            results.append({
                'prompt': prompt,
                **response
            })
    
    # Save results
    if output_file:
        with open(output_file, 'w') as f:
            json.dump(results, f, indent=2)
        print_colored(f"\n💾 Results saved to: {output_file}", Colors.GREEN)
    
    # Print summary
    stats = await router.get_stats()
    print_stats(stats)


async def cost_estimate_mode(router: AIRouter, requests_per_day: int):
    """Show cost estimates"""
    print_header("💰 Cost Estimation")
    
    estimate = await router.cost_tracker.estimate_monthly_cost(requests_per_day)
    
    print(f"Requests per day: {Colors.CYAN}{requests_per_day}{Colors.ENDC}")
    print(f"Avg cost per request: {Colors.GREEN}${estimate['avg_cost_per_request']:.4f}{Colors.ENDC}")
    print(f"\nEstimated daily cost: {Colors.YELLOW}${estimate['estimated_daily_cost']:.2f}{Colors.ENDC}")
    print(f"Estimated monthly cost: {Colors.BOLD}{Colors.GREEN}${estimate['estimated_monthly_cost']:.2f}{Colors.ENDC}")
    
    # Show comparison
    print_colored("\n📊 Cost Comparison:", Colors.BOLD)
    
    if estimate['estimated_monthly_cost'] > 0:
        # Estimate all-Opus cost
        opus_cost_per_request = 0.055  # Rough estimate
        opus_monthly = opus_cost_per_request * requests_per_day * 30
        savings = opus_monthly - estimate['estimated_monthly_cost']
        savings_pct = (savings / opus_monthly) * 100 if opus_monthly > 0 else 0
        
        print(f"All Claude Opus: ${opus_monthly:.2f}/month")
        print(f"With Router: ${estimate['estimated_monthly_cost']:.2f}/month")
        print(f"{Colors.GREEN}Savings: ${savings:.2f}/month ({savings_pct:.0f}%){Colors.ENDC}")


async def test_complexity_mode():
    """Test complexity analyzer"""
    print_header("🧪 Complexity Analyzer Test")
    
    from ai_router import ComplexityAnalyzer
    
    test_prompts = [
        ("Format this JSON", TaskComplexity.TRIVIAL),
        ("Write a CRUD endpoint", TaskComplexity.SIMPLE),
        ("Implement user authentication with JWT", TaskComplexity.MODERATE),
        ("Design a microservices architecture", TaskComplexity.COMPLEX),
        ("Fix production bug causing data loss", TaskComplexity.CRITICAL),
    ]
    
    for prompt, expected in test_prompts:
        complexity, confidence = ComplexityAnalyzer.analyze(prompt)
        match = "✅" if complexity == expected else "❌"
        
        color = Colors.GREEN if match == "✅" else Colors.RED
        print(f"{match} {color}{prompt[:50]}{Colors.ENDC}")
        print(f"   → {complexity.name} (confidence: {confidence:.2f})")
        print(f"   → Expected: {expected.name}\n")


def main():
    parser = argparse.ArgumentParser(
        description="AI Router CLI - Intelligent routing between AI models",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Interactive mode
  python router_cli.py
  
  # Single prompt
  python router_cli.py -p "Write a Python function to sort a list"
  
  # Force specific model
  python router_cli.py -p "Explain async/await" --model sonnet
  
  # Batch processing
  python router_cli.py --batch prompts.txt --output results.json
  
  # Show statistics
  python router_cli.py --stats
  
  # Cost estimation
  python router_cli.py --estimate 500
  
  # Test complexity analyzer
  python router_cli.py --test-complexity
        """
    )
    
    parser.add_argument(
        '-p', '--prompt',
        help='Single prompt to process'
    )
    
    parser.add_argument(
        '--model',
        choices=['opus', 'sonnet', 'haiku', 'deepseek'],
        help='Force specific model'
    )
    
    parser.add_argument(
        '--batch',
        help='Batch process prompts from file (one per line)'
    )
    
    parser.add_argument(
        '--output',
        help='Output file for batch results (JSON)'
    )
    
    parser.add_argument(
        '--stats',
        action='store_true',
        help='Show router statistics'
    )
    
    parser.add_argument(
        '--estimate',
        type=int,
        metavar='REQUESTS_PER_DAY',
        help='Estimate monthly costs for N requests per day'
    )
    
    parser.add_argument(
        '--strategy',
        choices=['balanced', 'conservative', 'cost-optimized'],
        default='balanced',
        help='Routing strategy (default: balanced)'
    )
    
    parser.add_argument(
        '--test-complexity',
        action='store_true',
        help='Test complexity analyzer'
    )
    
    args = parser.parse_args()
    
    # Test complexity analyzer (doesn't need router)
    if args.test_complexity:
        asyncio.run(test_complexity_mode())
        return
    
    # Select routing strategy
    strategy_map = {
        'balanced': BALANCED_ROUTING,
        'conservative': CONSERVATIVE_ROUTING,
        'cost-optimized': COST_OPTIMIZED_ROUTING,
    }
    routing_config = strategy_map[args.strategy]
    
    # Initialize router
    try:
        router = AIRouter(MODEL_CONFIGS, routing_config)
    except Exception as e:
        print_colored(f"❌ Failed to initialize router: {str(e)}", Colors.RED)
        print_colored("\nMake sure to:", Colors.YELLOW)
        print("1. Copy config_example.py to config.py")
        print("2. Fill in your API keys")
        print("3. Set environment variables if needed")
        sys.exit(1)
    
    try:
        # Route to appropriate mode
        if args.stats:
            stats = asyncio.run(router.get_stats())
            print_stats(stats)
        
        elif args.estimate:
            asyncio.run(cost_estimate_mode(router, args.estimate))
        
        elif args.batch:
            asyncio.run(batch_mode(router, args.batch, args.output))
        
        elif args.prompt:
            asyncio.run(single_prompt_mode(router, args.prompt, args.model))
        
        else:
            # Interactive mode
            asyncio.run(interactive_mode(router))
    
    finally:
        asyncio.run(router.cleanup())


if __name__ == '__main__':
    main()
