import {
  instrumentSchema,
  normalizePersian,
  orderBookSnapshotSchema,
  type Instrument,
  type MarketDataProvider,
  type OrderBookSnapshot,
} from './market.types.js';

// Explicit offline provider; its data is never promoted to verified market evidence.
export class FixtureMarketClient implements MarketDataProvider {
  private readonly books: OrderBookSnapshot[];
  constructor(fixtures: readonly unknown[]) {
    this.books = fixtures.map((value) => ({
      ...orderBookSnapshotSchema.parse(value),
      verified: false,
    }));
  }
  search(query: string): Promise<Instrument[]> {
    const text = normalizePersian(query);
    return Promise.resolve(
      this.books
        .filter((book) => book.instrument.symbol.includes(text))
        .map((book) => ({ ...book.instrument })),
    );
  }
  snapshot(value: Instrument): Promise<OrderBookSnapshot> {
    const instrument = instrumentSchema.parse(value);
    const book = this.books.find(
      (item) => item.instrument.insCode === instrument.insCode,
    );
    if (!book)
      return Promise.reject(
        new Error('Instrument not found in unverified fixtures'),
      );
    return Promise.resolve(structuredClone(book));
  }
}
