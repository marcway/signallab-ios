import Testing

@testable import SignalLab

struct LocalInstrumentCatalogTests {
    @Test
    func instrumentIdentifiersAreUnique() {
        let instruments = LocalInstrumentCatalog.instruments

        #expect(Set(instruments.map(\.id)).count == instruments.count)
    }

    @Test
    func exposesExpectedInitialInstruments() {
        let instruments = LocalInstrumentCatalog.instruments

        #expect(
            instruments
                == [
                    Instrument(id: "eurusd", symbol: "EURUSD", displayName: "EUR/USD"),
                    Instrument(id: "xauusd", symbol: "XAUUSD", displayName: "Gold"),
                    Instrument(id: "nas100", symbol: "NAS100", displayName: "Nasdaq 100"),
                ]
        )
    }
}
