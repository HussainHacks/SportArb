import asyncio
import json
from backend.collectors.odds_api_io import OddsApiIoCollector

async def main():
    # Pass empty list to prevent default from config 'ESPN BET' breaking it
    col = OddsApiIoCollector(bookmakers=[])
    events = await col.fetch_events(sport='basketball', limit=5)
    
    total_books = set()
    total_odds_lines = 0
    
    for event in events:
        odds = await col.fetch_odds(event['id'], bookmakers=[])
        if odds and 'bookmakers' in odds:
            b_dict = odds['bookmakers']
            total_books.update(b_dict.keys())
            for b_name, markets in b_dict.items():
                for m in markets:
                    total_odds_lines += len(m.get('odds', []))
            
            if b_dict:
                print(f"Event {event['id']}: Found {len(b_dict)} bookmakers: {list(b_dict.keys())}")
        
    print(f"\nTotal unique bookmakers found: {total_books}")
    print(f"Total separate odds list entries found: {total_odds_lines}")
    
    await col.close()

asyncio.run(main())
