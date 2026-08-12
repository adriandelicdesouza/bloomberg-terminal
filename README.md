# Open-Source Bloomberg Financial Terminal

A self-hosted financial analysis platform for market data, valuation, and equity research.

## Overview

This project is an open-source financial terminal designed to provide Bloomberg-style functionality through a lightweight, self-hosted application.

The platform focuses on:

* Market and company data
* Equity valuation
* Discounted Cash Flow (DCF) analysis
* Historical financial analysis
* Financial metrics and ratios
* Automated valuation calculations
* A web-based interface
* Local caching to reduce unnecessary API requests

## Features

### DCF Valuation

The DCF engine provides:

* Historical revenue analysis
* Revenue CAGR calculations
* Historical financial periods
* Revenue growth calculations
* Forecast-period assumptions
* Free cash flow projections
* Discounted cash flow calculations
* Terminal value
* Enterprise value
* Equity value
* Implied valuation metrics

Historical CAGR calculations use complete chronological data to ensure the correct number of periods is applied.

### Data

The application retrieves financial information from external market-data providers and processes it into normalized datasets for analysis.

Data caching is used where appropriate to improve performance and reduce repeated requests.

### Backend

The backend is written in Python and provides API endpoints for:

* Market data
* Company information
* Historical financials
* DCF calculations
* Valuation metrics

### Frontend

The frontend provides a terminal-style interface for navigating financial information and performing valuation analysis.

## Project Structure

```text
bloomberg-terminal/
├── backend/
│   ├── main.py
│   └── ...
├── frontend/
│   └── ...
├── tests/
│   ├── test_dcf_historical_periods.py
│   └── ...
├── .venv/
├── requirements.txt
└── README.md
```

## Development

### Clone the repository

```bash
git clone <repository-url>
cd bloomberg-terminal
```

### Create the virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

On Windows:

```powershell
.venv\Scripts\Activate.ps1
```

### Install dependencies

```bash
pip install -r requirements.txt
```

### Run the backend

```bash
python backend/main.py
```

## Testing

Run the test suite with:

```bash
pytest
```

Run the DCF historical-period tests specifically:

```bash
pytest tests/test_dcf_historical_periods.py
```

## DCF Historical Calculations

Historical growth calculations are based on chronologically ordered financial data.

For values:

```text
Year 1 → Year 2 → Year 3 → Year 4
```

the CAGR is calculated over three periods:

```text
CAGR = (Year 4 / Year 1)^(1 / 3) - 1
```

The implementation therefore distinguishes between the number of observations and the number of periods between those observations.

## Development Status

The project is under active development. Core valuation functionality is being developed and tested incrementally, with particular attention to calculation accuracy, historical-period handling, and reproducibility.

## Contributing

Contributions are welcome.

When submitting changes:

1. Keep calculations deterministic and reproducible.
2. Add regression tests for calculation changes.
3. Avoid modifying unrelated functionality.
4. Run the test suite before committing.
5. Document material changes to valuation methodology.

## License

This project is open source. See the repository license for the applicable terms.
